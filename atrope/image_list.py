# -*- coding: utf-8 -*-

# Copyright 2014 Alvaro Lopez Garcia
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import abc
import json
import logging
import os.path
import pprint

from oslo.config import cfg
import requests
import yaml

from atrope import endorser
from atrope import exception
from atrope import image
from atrope import paths
from atrope import smime
from atrope import utils

opts = [
    cfg.StrOpt('image_list_sources',
               default='/etc/atrope/lists.yaml',
               help='Where the image list sources are stored.'),
    cfg.StrOpt('cache_dir',
               default=paths.state_path_def('lists'),
               help='Where instances are stored on disk'),
]

cli_opts = [
    cfg.StrOpt('index',
               help="Show the configured image lists",
               positional=True),
]

CONF = cfg.CONF
CONF.register_opts(opts)
CONF.register_cli_opts(cli_opts, group='imagelist')

# FIXME(aloga): this should be configurable
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


class HepixImageList(object):
#hv:imagelist
    required_fields = (
        "dc:date:created",
        "dc:date:expires",
        "hv:endorser",
        "dc:identifier",
        "dc:description",
        "dc:title",
        "hv:images",
        "dc:source",
        "hv:version",
        "hv:uri",
    )

    def __init__(self, meta):
        meta = meta.get("hv:imagelist", {})
        keys = meta.keys()
        if not all([i in keys for i in self.required_fields]):
            reason = "Invalid image list, missing mandatory fields"
            raise exception.InvalidImageList(reason=reason)

        self.created = meta["dc:date:created"]
        self.expired = meta["dc:date:expires"]
        self.uuid = meta["dc:identifier"]
        self.description = meta["dc:description"]
        self.name = meta["dc:title"]
        self.source = meta["dc:source"]
        self.version = meta["hv:version"]
        self.uri = meta["hv:uri"]

        endorser_meta = meta.get("hv:endorser")
        self.endorser = endorser.Endorser(endorser_meta)


class ImageList(object):
    """An image list."""

    def __init__(self, name, url="", enabled=True, endorser={}, token=""):
        self.name = name

        self.url = url
        self.token = token

        self.enabled = enabled

        self.endorser = endorser
        self.signers = None
        self.verified = False
        self.trusted = False
        self.error = None

        self.contents = None
        self.d_contents = {}

        self.images = []

    def _set_error(func):
        def decorated(self):
            try:
                func(self)
            except Exception as e:
                self.error = e
                raise
        return decorated

    @_set_error
    def fetch(self):
        if self.enabled and self.url:
            self.contents = self._fetch()
            self.verified, self.signers, raw_list = self._verify()
            # FIXME(aloga): We should check that the JSON is valid, and that
            # load the data into the object.
            try:
                self.d_contents = json.loads(raw_list)
            except ValueError:
                raise exception.InvalidImageList(reason="Invalid JSON.")

            print "*"*90
            HepixImageList(self.d_contents)
            print "*"*90

            img_list = self.d_contents.get("hv:imagelist", {})
            for img in img_list.get("hv:images"):
                self.images.append(image.HepixImage(img))

            self.trusted = self._check_endorser()

    def __repr__(self):
        return "<%s: %s>" % (
            self.__class__.__name__,
            self.name
        )

    def _fetch(self):
        """
        Get the image list from the server.

        If it is needed, use a token to authenticate against the server.

        :returns: the image list.
        :raises: exception.ImageListDownloadFailed if it is not possible to get
                 the image.
        """
        if self.token:
            auth = (self.token, 'x-oauth-basic')
        else:
            auth = None
        response = requests.get(self.url, auth=auth)
        if response.status_code != 200:
            raise exception.ImageListDownloadFailed(code=response.status_code,
                                                    reason=response.reason)
        else:
            return response.content

    def _verify(self):
        """
        Verify the image list SMIME signature.

        :returns: tuple (signers, raw_list) with the signers and the raw list.
        :raises: exception.SMIMEValidationError if it is not possible to verify
                 the signature.
        """
        verifier = smime.SMIMEVerifier()
        try:
            signers, raw_list = verifier.verify(self.contents)
        except Exception:
            raise
        else:
            return True, signers, raw_list

    def _check_endorser(self):
        """
        Check the endorsers of an image list.

        :returns: True of False if endorsers are trusted or not.
        """

        # FIXME(aloga): This should be in its own class
        list_endorser = self.d_contents.get("hv:imagelist", {})
        list_endorser = list_endorser.get("hv:endorser", {})
        list_endorser = list_endorser.get("hv:x509", {})
        if not all(i in list_endorser for i in ("hv:ca", "hv:dn")):
            msg = "List '%s' does not contain a valid endorser" % self.name
            logging.error(msg)
            self.error = msg
            return False

        if self.endorser["dn"] != list_endorser["hv:dn"]:
            msg = ("List '%s' endorser is not trusted, DN mismatch %s != %s" %
                   (self.name, self.endorser["dn"], list_endorser["hv:dn"]))
            logging.error(msg)
            self.error = msg
            return False

        if self.endorser["ca"] != list_endorser["hv:ca"]:
            msg = ("List '%s' endorser CA is invalid %s != %s" %
                   (self.name, self.endorser["ca"], list_endorser["hv:ca"]))
            logging.error(msg)
            self.error = msg
            return False
        return True

    def print_list(self, contents=False):
        d = {
            "name": self.name,
            "url": self.url,
            "enabled": self.enabled,
            # FIXME(aloga): objectify endorser
            "endorser_dn": self.endorser.get("dn", None),
            "endorser_ca": self.endorser.get("ca", None),
        }
        d["verified"] = self.verified
        d["trusted"] = self.trusted
        d["token set"] = self.token and True
        if self.error is not None:
            d["error"] = self.error
        if self.contents is not None and contents:
            d["contents"] = pprint.pformat(self.d_contents)

        utils.print_dict(d)


class BaseImageListManager(object):
    __metaclass__ = abc.ABCMeta

    def __init__(self):
        self.configured_lists = {}
        self.loaded_lists = None

        self.cache_dir = os.path.abspath(CONF.cache_dir)
        utils.makedirs(self.cache_dir)

        self._load_sources()

    @abc.abstractmethod
    def _load_sources(self):
        """Load the image sources from disk."""

    @abc.abstractmethod
    def add_image_list_source(self, image):
        """Add an image source to the configuration file."""

    def _fetch_and_verify(self, l):
        """
        Fetch and verify an image list.

        If there are errors loading the list the appropriate attributes won't
        be set, so there is no need to fail here, but rather return the list.
        """
        try:
            l.fetch()
        except exception.AtropeException as e:
            logging.error("Error loading list '%s', reason: %s" %
                          (l.name, e.message))
            logging.debug("Exception while downloading list '%s'" % l.name,
                          exc_info=e)
        return l

    def fetch_list(self, image_list):
        """Get an individual list."""
        l = self.configured_lists.get(image_list)
        if l is None:
            raise exception.InvalidImageList(reason="not found in config")
        return self._fetch_and_verify(l)

    def fetch_lists(self):
        """Get all the configured lists."""
        all_lists = []
        for l in self.configured_lists.values():
            l = self._fetch_and_verify(l)
            all_lists.append(l)

        return all_lists

    def load_lists(self):
        if self.loaded_lists is None:
            self.loaded_lists = self.fetch_lists()

    def sync_cache(self):
        self.load_lists()

        valid_paths = [self.cache_dir]
        invalid_paths = []

        for l in self.loaded_lists:
            if l.enabled:
                basedir = os.path.join(self.cache_dir, l.name)
                valid_paths.append(basedir)
                imgdir = os.path.join(self.cache_dir, l.name, 'images')
                if l.trusted and l.verified:
                    utils.makedirs(imgdir)
                    valid_paths.append(imgdir)
                    for img in l.images:
                        try:
                            img.download(imgdir)
                        except exception.ImageVerificationFailed:
                            # FIXME(aloga): we should notify about this in the
                            # cmd line.
                            pass
                        else:
                            valid_paths.append(img.location)

        for root, dirs, files in os.walk(self.cache_dir):
            if root not in valid_paths:
                invalid_paths.append(root)
            for i in files + dirs:
                i = os.path.join(root, i)
                if i not in valid_paths:
                    invalid_paths.append(i)

        logging.debug("Removing %s from cache directory." % invalid_paths)
        utils.rmtree(basedir)


class YamlImageListManager(BaseImageListManager):
    def __init__(self):
        super(YamlImageListManager, self).__init__()

    def _load_sources(self):
        with open(CONF.image_list_sources, "rb") as f:
            image_lists = yaml.safe_load(f)

        for name, list_meta in image_lists.iteritems():
            l = ImageList(name,
                          url=list_meta.get("url", ""),
                          enabled=list_meta.get("enabled", True),
                          endorser=list_meta.get("endorser", {}),
                          token=list_meta.get("token", ""))
            self.configured_lists[name] = l

    def add_image_list_source(self, image_list, force=False):
        if image_list.name in self.configured_lists and not force:
            raise exception.DuplicatedImageList(id=image_list.name)

        self.configured_lists[image_list.name] = image_list

    def write_image_list_sources(self):
        lists = {}
        for name, image_list in self.configured_lists.iteritems():
            lists[name] = {"url": image_list.url,
                           "enabled": image_list.enabled,
                           "endorser": image_list.endorser,
                           "token": image_list.token}
        dump = yaml.dump(lists)
        if not dump:
            raise exception.AtropeException()

        with open(CONF.image_list_sources, "w") as f:
            f.write(dump)

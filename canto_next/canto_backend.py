# -*- coding: utf-8 -*-
#Canto - RSS reader backend
#   Copyright (C) 2010 Jack Miller <jack@codezen.org>
#
#   This program is free software; you can redistribute it and/or modify
#   it under the terms of the GNU General Public License version 2 as 
#   published by the Free Software Foundation.

# This Backend class is the core of the daemon's specific protocol.

from feed import allfeeds
from encoding import encoder, decoder
from protect import protection
from server import CantoServer
from config import CantoConfig
from storage import CantoShelf
from fetch import CantoFetch
from hooks import on_hook, call_hook
from tag import alltags
from format import escsplit

import traceback
import logging
import signal
import getopt
import Queue
import fcntl
import errno
import time
import sys
import os

# By default this will log to stderr.
logging.basicConfig(
        filemode = "w",
        format = "%(asctime)s : %(name)s -> %(message)s",
        datefmt = "%H:%M:%S",
        level = logging.INFO
)

log = logging.getLogger("CANTO-DAEMON")

class CantoBackend(CantoServer):

    def init(self):
        # Log verbosity
        self.verbosity = 0

        # Shelf for feeds:
        self.fetch = None
        self.fetch_timer = 0

        self.watches = { "new_tags" : [],
                         "del_tags" : [],
                         "config" : [],
                         "tags" : {} }

        self.shelf = None

        # No bad arguments.
        if self.args():
            sys.exit(-1)

        # No invalid paths.
        if self.ensure_paths():
            sys.exit(-1)

        # Get pid lock.
        if self.pid_lock():
            sys.exit(-1)

        # Previous to this line, all output is just error messages to stderr.
        self.set_log()

        # Initial log chatter.
        log.info("Canto Daemon started.")

        if self.verbosity:
            rootlog = logging.getLogger()
            rootlog.setLevel(max(rootlog.level - 10 * self.verbosity,0))
            log.info("verbosity = %d" % self.verbosity)

        log.info("conf_dir = %s" % self.conf_dir)

        # Actual start.
        self.get_storage()
        self.get_config()
        self.get_fetch()

        self.setup_hooks()

        CantoServer.__init__(self, self.conf_dir + "/.canto_socket",\
                Queue.Queue())

        # Signal handlers kickoff after everything else is init'd
        self.alarmed = 0
        signal.signal(signal.SIGALRM, self.sig_alrm)
        signal.alarm(1)

    def check_dead_feeds(self):
        for URL in allfeeds.dead_feeds.keys():
            feed = allfeeds.dead_feeds[URL]
            for item in feed.items:
                if protection.protected(item["id"]):
                    log.debug("Dead feed %s still committed." % feed.URL)
                    break
            else:
                allfeeds.really_dead(feed)

    # Propagate config changes to watching sockets.

    def on_config_change(self, change):
        for socket in self.watches["config"]:
            self.cmd_configs(socket, change.keys())

        pretags = alltags.tags.keys()
        newtags = []
        oldtags = []

        self.conf.parse()

        for tagname in alltags.tags.keys():
            if tagname not in pretags:
                newtags.append(tagname)

        for tagname in pretags:
            if tagname not in alltags.tags:
                oldtags.append(tagname)

        if newtags:
            call_hook("new_tag", [ newtags ])
        if oldtags:
            call_hook("del_tag", [ oldtags ])

        self.check_dead_feeds()

    # Notify clients of new tags.

    def on_new_tag(self, tags):
        for socket in self.watches["new_tags"]:
            self.write(socket, "NEWTAGS", tags)

    # Propagate tag changes to watching sockets.

    def on_tag_change(self, tag):
        if tag in self.watches["tags"]:
            for socket in self.watches["tags"][tag]:
                self.write(socket, "TAGCHANGE", tag)

    # Notify clients of dead tags:

    def on_del_tag(self, tags):
        for socket in self.watches["del_tags"]:
            self.write(socket, "DELTAGS", tags)

    # If a socket dies, it's not longer watching any events and
    # revoke any protection associated with it

    def on_kill_socket(self, socket):
        while socket in self.watches["config"]:
            self.watches["config"].remove(socket)

        while socket in self.watches["new_tags"]:
            self.watches["new_tags"].remove(socket)

        while socket in self.watches["del_tags"]:
            self.watches["del_tags"].remove(socket)

        for tag in self.watches["tags"]:
            while socket in self.watches["tags"][tag]:
                self.watches["tags"][tag].remove(socket)

        protection.unprotect((socket, "auto"))
        self.check_dead_feeds()

    # We need to be alerted on certain events, ensure
    # we get notified about them.

    def setup_hooks(self):
        on_hook("new_tag", self.on_new_tag)
        on_hook("del_tag", self.on_del_tag)
        on_hook("config_change", self.on_config_change)
        on_hook("tag_change", self.on_tag_change)
        on_hook("kill_socket", self.on_kill_socket)

    # Return list of item tuples after global transforms have
    # been performed on them.

    def apply_transforms(self, tag):
        if self.conf.global_transform:
            return self.conf.global_transform(tag)
        return tag

    # PING -> PONG

    def cmd_ping(self, socket, args):
        self.write(socket, "PONG", u"")

    # LISTFEEDS -> [ (tag, URL) for all feeds ]

    def cmd_listfeeds(self, socket, args):
        feeds = []
        for feed in self.conf.feeds:
            feeds.append((feed.name, feed.URL))
        self.write(socket, "LISTFEEDS", feeds)

    # LISTTRANSFORMS -> [ { "name" : " " } for all defined filters ]

    def cmd_listtransforms(self, socket, args):
        transforms = []
        for transform in self.conf.transforms:
            transforms.append({"name" : transform["name"]})
        self.write(socket, "LISTTRANSFORMS", transforms)

    # ITEMS [tags] -> { tag : [ ids ], tag2 : ... }

    def cmd_items(self, socket, args):
        ids = []
        response = {}

        for tag in args:
            # get_tag returns a list invariably, but may be empty.
            response[tag] = self.apply_transforms(alltags.get_tag(tag))

            # ITEMS must protect all given items automatically to
            # avoid instances where an item disappears before a PROTECT
            # call can be made by the client.

            protection.protect((socket, "auto"), response[tag])

        self.write(socket, "ITEMS", response)

    # ATTRIBUTES { id : [ attribs .. ] .. } ->
    # { id : { attribute : value } ... }

    def cmd_attributes(self, socket, args):
        ret = {}
        feeds = allfeeds.items_to_feeds(args.keys())
        for f in feeds:
            ret.update(f.get_attributes(feeds[f], args))

        self.write(socket, "ATTRIBUTES", ret)

    # SETATTRIBUTES { id : { attribute : value } ... } -> None

    def cmd_setattributes(self, socket, args):
        ret = {}
        feeds = allfeeds.items_to_feeds(args.keys())
        for f in feeds:
            f.set_attributes(feeds[f], args)

    # CONFIGS [ config.options ] -> { "option" : "value" ... }

    def cmd_configs(self, socket, args):
        if args:
            ret = {}
            for opt in args:
                section, setting = escsplit(opt, ".")
                if not setting:
                    ret[opt] = self.conf.get_section(opt)
                    continue

                try:
                    val = self.conf.get("", section, setting, None, 0)
                    if section in ret:
                        ret[section].update({ setting : val })
                    else:
                        ret[section] = { setting : val }
                except:
                    log.error("Exception getting option %s" % opt)
        else:
            ret = self.conf.get_sections()

        self.write(socket, "CONFIGS", ret)

    # SETCONFIGS { "section" : {"option" : "value" } ... }

    def cmd_setconfigs(self, socket, args):
        changes = {}
        for section in args.keys():
            if not args[section] and self.conf.has_section(section):
                self.conf.remove_section(section)
                continue

            for setting in args[section]:
                self.conf.set(section, setting, args[section][setting])
                changes.update({ section :\
                        { setting : args[section][setting]}})

        self.conf.write()
        call_hook("config_change", [changes])

    # WATCHCONFIGS

    def cmd_watchconfigs(self, socket, args):
        self.watches["config"].append(socket)

    # WATCHNEWTAGS

    def cmd_watchnewtags(self, socket, args):
        self.watches["new_tags"].append(socket)

    # WATCHDELTAGS

    def cmd_watchdeltags(self, socket, args):
        self.watches["del_tags"].append(socket)

    # WATCHTAGS [ "tag", ... ]

    def cmd_watchtags(self, socket, args):
        for tag in args:
            log.debug("socket %s watching tag %s" % (socket, tag))
            if tag in self.watches["tags"]:
                self.watches["tags"][tag].append(socket)
            else:
                self.watches["tags"][tag] = [socket]

    # PROTECT { "reason" : [ id, ... ], ... }

    def cmd_protect(self, socket, args):
        for reason in args:
            protection.protect((socket, reason), args[reason])

    # UNPROTECT { "reason" : [ id, ... ], ... }

    def cmd_unprotect(self, socket, args):
        for reason in args:
            for id in args[reason]:
                protection.unprotect_one((socket, reason), id)

    # The workhorse that maps all requests to their handlers.
    def run(self):
        while 1:
            if not self.queue.empty():
                (socket, (cmd, args)) = self.queue.get()

                if cmd == "DIE":
                    log.info("Received DIE.")
                    return

                cmdf = "cmd_" + cmd.lower()
                if hasattr(self, cmdf):
                    func = getattr(self, cmdf)
                    func(socket, args)
                else:
                    log.info("Got unknown command: %s" % (cmd))

            self.check_conns()

            # Process any possible feed updates.
            self.fetch.process()

            if self.alarmed:
                # Decrement all timers
                self.fetch_timer -= 1

                if self.verbosity > 1:
                    log.debug("Alarmed.")

                # Check whether feeds need to be updated and fetch
                # them if necessary.

                if self.fetch_timer <= 0:
                    self.fetch.fetch()
                    self.fetch_timer = 60

                self.alarmed = False

            time.sleep(0.01)

    # This function parses and validates all of the command line arguments.
    def args(self):
        try:
            optlist = getopt.getopt(sys.argv[1:], 'D:v', ["dir="])[0]
        except getopt.GetoptError, e:
            log.error("Error: %s" % e.msg)
            return -1

        self.conf_dir = os.path.expanduser(u"~/.canto-ng/")

        for opt, arg in optlist:
            # -D base configuration directory. Highest priority.
            if opt in ["-D", "--dir"]:
                self.conf_dir = os.path.expanduser(decoder(arg))
                self.conf_dir = os.path.realpath(self.conf_dir)

            # -v increase verbosity
            elif opt in ["-v"]:
                self.verbosity += 1

        return 0

    def sig_alrm(self, a, b):
        self.alarmed = 1
        signal.alarm(1)

    # This function makes sure that the configuration paths are all R/W or
    # creatable.

    def ensure_paths(self):
        if os.path.exists(self.conf_dir):
            if not os.path.isdir(self.conf_dir):
                log.error("Error: %s is not a directory." % self.conf_dir)
                return -1
            if not os.access(self.conf_dir, os.R_OK):
                log.error("Error: %s is not readable." % self.conf_dir)
                return -1
            if not os.access(self.conf_dir, os.W_OK):
                log.error("Error: %s is not writable." % self.conf_dir)
                return -1
        else:
            try:
                os.makedirs(self.conf_dir)
            except e:
                log.error("Exception making %s : %s" % (self.conf_dir, e.msg))
                return -1
        return self.ensure_files()

    def ensure_files(self):
        for f in [ "feeds", "conf", "daemon-log", "pid"]:
            p = self.conf_dir + "/" + f
            if os.path.exists(p):
                if not os.path.isfile(p):
                    log.error("Error: %s is not a file." % p)
                    return -1
                if not os.access(p, os.R_OK):
                    log.error("Error: %s is not readable." % p)
                    return -1
                if not os.access(p, os.W_OK):
                    log.error("Error: %s is not writable." % p)
                    return -1

        # These paths are now guaranteed to read/writable.

        self.feed_path = self.conf_dir + "/feeds"
        self.pid_path = self.conf_dir + "/pid"
        self.log_path = self.conf_dir + "/daemon-log"
        self.conf_path = self.conf_dir + "/conf"

        return None

    def pid_lock(self):
        self.pidfile = open(self.pid_path, "a+")
        try:
            fcntl.flock(self.pidfile.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.pidfile.seek(0, 0)
            self.pidfile.truncate()
            self.pidfile.write("%d" % os.getpid())
            self.pidfile.flush()
        except IOError, e:
            if e.errno == errno.EAGAIN:
                log.error("Error: Another canto-daemon is running here.")
                return -1
            raise
        return None

    def pid_unlock(self):
        log.debug("Unlocking pidfile.")
        fcntl.flock(self.pidfile.fileno(), fcntl.LOCK_UN)
        self.pidfile.close()
        log.debug("Unlocked.")

    # Reset basic log info to log to the right file.
    def set_log(self):
        f = open(self.log_path, "w")
        os.dup2(f.fileno(), sys.stderr.fileno())

    # Bring up storage, the only errors possible at this point are 
    # fatal and handled lower in CantoShelf.

    def get_storage(self):
        self.shelf = CantoShelf(self.feed_path)

    # Bring up config, the only errors possible at this point will
    # be fatal and handled lower in CantoConfig.

    def get_config(self):
        self.conf = CantoConfig(self.conf_path, self.shelf)
        self.conf.parse()

    def get_fetch(self):
        self.fetch = CantoFetch(self.shelf, self.conf)
        self.fetch.fetch()

    def start(self):
        try:
            self.init()
            self.run()
            log.info("Exiting cleanly.")

        # Cleanly shutdown on ^C.
        except KeyboardInterrupt:
            pass

        # Pretty print any non-Keyboard exceptions.
        except Exception, e:
            tb = traceback.format_exc(e)
            log.error("Exiting on exception:")
            log.error("\n" + "".join(tb))

        self.exit()
        self.pid_unlock()
        sys.exit(0)

    def __init__(self):
        self.start()

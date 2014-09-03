# -*- coding: utf-8 -*-

#Canto - RSS reader backend
#   Copyright (C) 2010 Jack Miller <jack@codezen.org>
#
#   This program is free software; you can redistribute it and/or modify
#   it under the terms of the GNU General Public License version 2 as 
#   published by the Free Software Foundation.

from .feed import wlock_feeds
from .hooks import on_hook, call_hook

import threading
import traceback
import logging
import shelve
import time
import dbm
import sys
import os

log = logging.getLogger("SHELF")

class CantoShelf():
    def __init__(self, filename, writeback):
        self.writeback = writeback
        self.filename = filename

        self.open()

        # Sync after a block of requests has been fulfilled,
        # close the database all together on exit.

        on_hook("daemon_work_done", self.sync)
        on_hook("daemon_exit", self.close)

    def open(self):
        call_hook("daemon_db_open", [self.filename])

        mode = 'c'
        if dbm.whichdb(self.filename) == 'dbm.gnu':
            mode += 'u'

        if self.writeback:
            self.shelf = shelve.open(self.filename, mode, None, True)
        else:
            self.shelf = shelve.open(self.filename, mode)

    def __setitem__(self, name, value):
        self.shelf[name] = value

    def __getitem__(self, name):
        r = self.shelf[name]
        return r

    def __contains__(self, name):
        return name in self.shelf

    def __delitem__(self, name):
        del self.shelf[name]

    @wlock_feeds
    def sync(self):
        self.shelf.sync()

    def trim(self):
        log.debug("Attempting to trim...")
        self.close()
        self.open()

    def _reorganize(self):
        # This is a workaround for shelves implemented with database types
        # (like gdbm) that won't shrink themselves.

        # Because we're a delete heavy workload (as we drop items that are no
        # longer relevant), we check for reorganize() and use it on close,
        # which should shrink the DB and keep it from growing into perpetuity.

        try:
            db = dbm.open(self.filename, "wu")
            getattr(db, 'reorganize')()
            db.close()
        except Exception as e:
            log.warn("Failed to reorganize db:")
            log.warn(traceback.format_exc())
        else:
            log.debug("Successfully trimmed db")

    def close(self):
        self.shelf.sync()
        self.shelf.close()
        if dbm.whichdb(self.filename) == 'dbm.gnu':
            self._reorganize()
        self.shelf = None
        call_hook("daemon_db_close", [self.filename])

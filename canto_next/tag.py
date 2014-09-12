#Canto - RSS reader backend
#   Copyright (C) 2010 Jack Miller <jack@codezen.org>
#
#   This program is free software; you can redistribute it and/or modify
#   it under the terms of the GNU General Public License version 2 as 
#   published by the Free Software Foundation.

from .hooks import on_hook, call_hook
from .locks import tag_lock

import logging

log = logging.getLogger("TAG")

class CantoTags():
    def __init__(self):
        self.tags = {}
        self.changed_tags = []

        # Per-tag transforms
        self.tag_transforms = {}

        # Extra tag map
        # This allows tags to be defined as parts of larger tags.  For example,
        # Penny Arcade and xkcd could both have the extra "comic" tag which
        # could then be used in a filter to implement categories.

        self.extra_tags = {}

        # Batch tag_changes to be sent only after
        # a block of requests.

        on_hook("daemon_work_done", self.do_tag_changes)

    def items_to_tags(self, ids):
        tags = []
        for id in ids:
            for tag in self.tags:
                if id in self.tags[tag] and tag not in tags:
                    tags.append(tag)
        return tags

    def tag_changed(self, tag):
        if tag not in self.changed_tags:
            self.changed_tags.append(tag)

    def get_tag(self, tag):
        if tag in list(self.tags.keys()):
            return self.tags[tag]
        return []

    def get_tags(self):
        return list(self.tags.keys())

    def tag_transform(self, tag, transform):
        self.tag_transforms[tag] = transform

    def set_extra_tags(self, tag, extra_tags):
        self.extra_tags[tag] = extra_tags

    def reset(self):
        self.tag_transforms = {}
        self.extra_tags = {}

        for tag in self.tags:
            self.tag_changed(tag)
        self.tags = {}

    #
    # Following must be called with tag_lock held with write
    #

    def add_tag(self, id, name):
        if name in self.extra_tags:
            extras = self.extra_tags[name]
        else:
            extras = []

        alladded = [ name ] + extras

        for name in alladded:
            # Create tag if no tag exists
            if name not in self.tags:
                self.tags[name] = []
                call_hook("daemon_new_tag", [[ name ]])

            # Add to tag.
            if id not in self.tags[name]:
                self.tags[name].append(id)
                self.tag_changed(name)

    def remove_tag(self, id, name):
        if name in self.tags and id in self.tags[name]:
            self.tags[name].remove(id)
            self.tag_changed(name)

    def remove_id(self, id):
        for tag in self.tags:
            if id in self.tags[tag]:
                self.tags[tag].remove(id)
                self.tag_changed(tag)

    #
    # This is called from a hook, so it has to get the lock itself
    #

    def do_tag_changes(self):
        tag_lock.acquire_write()
        for tag in self.changed_tags:
            call_hook("daemon_tag_change", [ tag ])
        self.changed_tags = []
        tag_lock.release_write()

alltags = CantoTags()

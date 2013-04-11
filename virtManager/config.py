#
# Copyright (C) 2006, 2012 Red Hat, Inc.
# Copyright (C) 2006 Daniel P. Berrange <berrange@redhat.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
# MA 02110-1301 USA.
#
import os
import logging

from gi.repository import Gtk
from gi.repository import GConf

import virtinst
from virtinst import uriutil

from virtManager.keyring import vmmKeyring, vmmSecret


class vmmConfig(object):

    # GConf directory names for saving last used paths
    CONFIG_DIR_IMAGE = "image"
    CONFIG_DIR_ISO_MEDIA = "isomedia"
    CONFIG_DIR_FLOPPY_MEDIA = "floppymedia"
    CONFIG_DIR_SAVE = "save"
    CONFIG_DIR_RESTORE = "restore"
    CONFIG_DIR_SCREENSHOT = "screenshot"
    CONFIG_DIR_FS = "fs"

    # Metadata mapping for browse types. Prob shouldn't go here, but works
    # for now.
    browse_reason_data = {
        CONFIG_DIR_IMAGE : {
            "enable_create" : True,
            "storage_title" : _("Locate or create storage volume"),
            "local_title"   : _("Locate existing storage"),
            "dialog_type"   : Gtk.FileChooserAction.SAVE,
            "choose_button" : Gtk.STOCK_OPEN,
        },

        CONFIG_DIR_ISO_MEDIA : {
            "enable_create" : False,
            "storage_title" : _("Locate ISO media volume"),
            "local_title"   : _("Locate ISO media"),
        },

        CONFIG_DIR_FLOPPY_MEDIA : {
            "enable_create" : False,
            "storage_title" : _("Locate floppy media volume"),
            "local_title"   : _("Locate floppy media"),
        },

        CONFIG_DIR_FS : {
            "enable_create" : False,
            "storage_title" : _("Locate directory volume"),
            "local_title"   : _("Locate directory volume"),
            "dialog_type"   : Gtk.FileChooserAction.SELECT_FOLDER,
        },
    }

    CONSOLE_SCALE_NEVER = 0
    CONSOLE_SCALE_FULLSCREEN = 1
    CONSOLE_SCALE_ALWAYS = 2

    CONSOLE_KEYGRAB_NEVER = 0
    CONSOLE_KEYGRAB_FULLSCREEN = 1
    CONSOLE_KEYGRAB_MOUSEOVER = 2

    _PEROBJ_FUNC_SET    = 0
    _PEROBJ_FUNC_GET    = 1
    _PEROBJ_FUNC_LISTEN = 2

    DEFAULT_XEN_IMAGE_DIR = "/var/lib/xen/images"
    DEFAULT_XEN_SAVE_DIR = "/var/lib/xen/dump"

    DEFAULT_VIRT_IMAGE_DIR = "/var/lib/libvirt/images"
    DEFAULT_VIRT_SAVE_DIR = "/var/lib/libvirt"

    def __init__(self, appname, appversion, ui_dir, test_first_run=False):
        self.appname = appname
        self.appversion = appversion
        self.conf_dir = "/apps/" + appname
        self.ui_dir = ui_dir
        self.test_first_run = bool(test_first_run)

        self.conf = GConf.Client.get_default()
        self.conf.add_dir(self.conf_dir, GConf.ClientPreloadType.PRELOAD_NONE)

        # We don't create it straight away, since we don't want
        # to block the app pending user authorizaation to access
        # the keyring
        self.keyring = None

        self.default_qemu_user = "root"

        # Use this key to disable certain features not supported on RHEL
        self.rhel6_defaults = True
        self.preferred_distros = []
        self.hv_packages = []
        self.libvirt_packages = []
        self.askpass_package = []
        self.default_graphics_from_config = "vnc"

        self._objects = []

        self.support_threading = virtinst.support.support_threading()

        self.support_inspection = self.check_inspection(self.support_threading)

        self._spice_error = None

    def get_string_list(self, path):
        val = self.conf.get(path)
        if val is None:
            return None
        values = []
        for v in val.get_list():
            values.append(v.get_string())
        return values

    def set_string_list(self, path, values):
        newValues = []
        for v in values:
            nv = GConf.Value.new(GConf.ValueType.STRING)
            nv.set_string(v)
            newValues.append(nv)
        ignore = path
        #XXX: set_list is not available with introspection
        # val = GConf.Value()
        #val.set_list(newValues)
        #self.conf.set(path, val)

    def check_inspection(self, support_threading):
        if not support_threading:
            return False

        try:
            # Check we can open the Python guestfs module.
            from guestfs import GuestFS
            g = GuestFS()

            # Check for the first version which fixed Python GIL bug.
            version = g.version()
            if version["major"] == 1: # major must be 1
                if version["minor"] == 8:
                    if version["release"] >= 6: # >= 1.8.6
                        return True
                elif version["minor"] == 10:
                    if version["release"] >= 1: # >= 1.10.1
                        return True
                elif version["minor"] == 11:
                    if version["release"] >= 2: # >= 1.11.2
                        return True
                elif version["minor"] >= 12:    # >= 1.12, 1.13, etc.
                    return True
        except:
            pass

        return False


    # General app wide helpers (gconf agnostic)

    def get_appname(self):
        return self.appname
    def get_appversion(self):
        return self.appversion
    def get_ui_dir(self):
        return self.ui_dir

    def embeddable_graphics(self):
        ret = ["vnc", "spice"]
        return ret

    def remove_notifier(self, h):
        self.conf.notify_remove(h)

    # Used for debugging reference leaks, we keep track of all objects
    # come and go so we can do a leak report at app shutdown
    def add_object(self, obj):
        self._objects.append(obj)
    def remove_object(self, obj):
        self._objects.remove(obj)
    def get_objects(self):
        return self._objects[:]

    # Per-VM/Connection/Connection Host Option dealings
    def _perconn_helper(self, uri, pref_func, func_type, value=None):
        suffix = "connection_prefs/%s" % GConf.escape_key(uri, len(uri))
        return self._perobj_helper(suffix, pref_func, func_type, value)
    def _perhost_helper(self, uri, pref_func, func_type, value=None):
        host = uriutil.get_uri_hostname(uri)
        if not host:
            host = "localhost"
        suffix = "connection_prefs/hosts/%s" % host
        return self._perobj_helper(suffix, pref_func, func_type, value)
    def _pervm_helper(self, uri, uuid, pref_func, func_type, value=None):
        suffix = ("connection_prefs/%s/vms/%s" %
                  (GConf.escape_key(uri, len(uri)), uuid))
        return self._perobj_helper(suffix, pref_func, func_type, value)

    def _perobj_helper(self, suffix, pref_func, func_type, value=None):
        # This function wraps the regular preference setting functions,
        # replacing conf_dir with a connection, host, or vm specific path. For
        # VMs, the path is:
        #
        # conf_dir/connection_prefs/{CONN_URI}/vms/{VM_UUID}
        #
        # So a per-VM pref will look like
        # .../connection_prefs/qemu:---system/vms/1234.../console/scaling
        #
        # Yeah this is evil but it's also nice and easy :)

        oldconf = self.conf_dir
        newconf = oldconf

        # Don't make a bogus gconf path if this is called nested.
        if not oldconf.count(suffix):
            newconf = "%s/%s" % (oldconf, suffix)

        ret = None
        try:
            self.conf_dir = newconf
            if func_type == self._PEROBJ_FUNC_SET:
                if type(value) is not tuple:
                    value = (value,)
                pref_func(*value)
            elif func_type == self._PEROBJ_FUNC_GET:
                ret = pref_func()
            elif func_type == self._PEROBJ_FUNC_LISTEN:
                ret = pref_func(value)
        finally:
            self.conf_dir = oldconf

        return ret

    def set_pervm(self, uri, uuid, pref_func, args):
        """
        @param uri: VM connection URI
        @param uuid: VM UUID
        @param value: Set value or listener callback function
        @param pref_func: Global preference get/set/listen func that the
                          pervm instance will overshadow
        """
        self._pervm_helper(uri, uuid, pref_func, self._PEROBJ_FUNC_SET, args)
    def get_pervm(self, uri, uuid, pref_func):
        ret = self._pervm_helper(uri, uuid, pref_func, self._PEROBJ_FUNC_GET)
        if ret is None:
            # If the GConf value is unset, return the global default.
            ret = pref_func()
        return ret
    def listen_pervm(self, uri, uuid, pref_func, cb):
        return self._pervm_helper(uri, uuid, pref_func,
                                  self._PEROBJ_FUNC_LISTEN, cb)

    def set_perconn(self, uri, pref_func, value):
        self._perconn_helper(uri, pref_func, self._PEROBJ_FUNC_SET, value)
    def get_perconn(self, uri, pref_func):
        ret = self._perconn_helper(uri, pref_func, self._PEROBJ_FUNC_GET)
        if ret is None:
            # If the GConf value is unset, return the global default.
            ret = pref_func()
        return ret
    def listen_perconn(self, uri, pref_func, cb):
        return self._perconn_helper(uri, pref_func,
                                    self._PEROBJ_FUNC_LISTEN, cb)

    def set_perhost(self, uri, pref_func, value):
        self._perhost_helper(uri, pref_func, self._PEROBJ_FUNC_SET, value)
    def get_perhost(self, uri, pref_func):
        ret = self._perhost_helper(uri, pref_func, self._PEROBJ_FUNC_GET)
        if ret is None:
            # If the GConf value is unset, return the global default.
            ret = pref_func()
        return ret
    def listen_perhost(self, uri, pref_func, cb):
        return self._perhost_helper(uri, pref_func,
                                    self._PEROBJ_FUNC_LISTEN, cb)

    def reconcile_vm_entries(self, uri, current_vms):
        """
        Remove any old VM preference entries for the passed URI
        """
        uri = GConf.escape_key(uri, len(uri))
        key = self.conf_dir + "/connection_prefs/%s/vms" % uri
        kill_vms = []
        gconf_vms = map(lambda inp: inp.split("/")[-1],
                        self.conf.all_dirs(key))

        for uuid in gconf_vms:
            if len(uuid) == 36 and not uuid in current_vms:
                kill_vms.append(uuid)

        for uuid in kill_vms:
            self.conf.recursive_unset(key + "/%s" % uuid, 0)

        if kill_vms:
            # Suggest gconf syncs, so that the unset dirs are fully removed
            self.conf.suggest_sync()

    #########################
    # General GConf helpers #
    #########################

    # Manager stats view preferences
    def is_vmlist_guest_cpu_usage_visible(self):
        return self.conf.get_bool(self.conf_dir + "/vmlist-fields/cpu_usage")
    def is_vmlist_host_cpu_usage_visible(self):
        return self.conf.get_bool(self.conf_dir +
                                  "/vmlist-fields/host_cpu_usage")
    def is_vmlist_disk_io_visible(self):
        return self.conf.get_bool(self.conf_dir + "/vmlist-fields/disk_usage")
    def is_vmlist_network_traffic_visible(self):
        return self.conf.get_bool(self.conf_dir +
                                  "/vmlist-fields/network_traffic")

    def set_vmlist_guest_cpu_usage_visible(self, state):
        self.conf.set_bool(self.conf_dir + "/vmlist-fields/cpu_usage", state)
    def set_vmlist_host_cpu_usage_visible(self, state):
        self.conf.set_bool(self.conf_dir + "/vmlist-fields/host_cpu_usage",
                           state)
    def set_vmlist_disk_io_visible(self, state):
        self.conf.set_bool(self.conf_dir + "/vmlist-fields/disk_usage", state)
    def set_vmlist_network_traffic_visible(self, state):
        self.conf.set_bool(self.conf_dir + "/vmlist-fields/network_traffic",
                           state)

    def on_vmlist_guest_cpu_usage_visible_changed(self, cb, userdata=None):
        return self.conf.notify_add(self.conf_dir + "/vmlist-fields/cpu_usage",
                                    cb, userdata)
    def on_vmlist_host_cpu_usage_visible_changed(self, cb, userdata=None):
        return self.conf.notify_add(self.conf_dir +
                                    "/vmlist-fields/host_cpu_usage", cb, userdata)
    def on_vmlist_disk_io_visible_changed(self, cb, userdata=None):
        return self.conf.notify_add(self.conf_dir + "/vmlist-fields/disk_usage",
                                    cb, userdata)
    def on_vmlist_network_traffic_visible_changed(self, cb, userdata=None):
        return self.conf.notify_add(
                        self.conf_dir + "/vmlist-fields/network_traffic", cb, userdata)

    # Keys preferences
    def get_keys_combination(self):
        ret = self.conf.get_string(self.conf_dir + "/keys/grab-keys")
        if not ret:
            # Left Control + Left Alt
            return "65507,65513"
        return ret
    def set_keys_combination(self, val):
        # Val have to be a list of integers
        val = ','.join(map(str, val))
        self.conf.set_string(self.conf_dir + "/keys/grab-keys", val)
    def on_keys_combination_changed(self, cb, userdata=None):
        return self.conf.notify_add(self.conf_dir + "/keys/grab-keys", cb, userdata)

    # Confirmation preferences
    def get_confirm_forcepoweroff(self):
        return self.conf.get_bool(self.conf_dir + "/confirm/forcepoweroff")
    def get_confirm_poweroff(self):
        return self.conf.get_bool(self.conf_dir + "/confirm/poweroff")
    def get_confirm_pause(self):
        return self.conf.get_bool(self.conf_dir + "/confirm/pause")
    def get_confirm_removedev(self):
        return self.conf.get_bool(self.conf_dir + "/confirm/removedev")
    def get_confirm_interface(self):
        return self.conf.get_bool(self.conf_dir + "/confirm/interface_power")
    def get_confirm_unapplied(self):
        return self.conf.get_bool(self.conf_dir + "/confirm/unapplied_dev")
    def get_confirm_delstorage(self):
        # If no schema is installed, we _really_ want this to default to True
        path = self.conf_dir + "/confirm/delete_storage"
        ret = self.conf.get(path)
        if ret is None:
            return True
        return self.conf.get_bool(path)


    def set_confirm_forcepoweroff(self, val):
        self.conf.set_bool(self.conf_dir + "/confirm/forcepoweroff", val)
    def set_confirm_poweroff(self, val):
        self.conf.set_bool(self.conf_dir + "/confirm/poweroff", val)
    def set_confirm_pause(self, val):
        self.conf.set_bool(self.conf_dir + "/confirm/pause", val)
    def set_confirm_removedev(self, val):
        self.conf.set_bool(self.conf_dir + "/confirm/removedev", val)
    def set_confirm_interface(self, val):
        self.conf.set_bool(self.conf_dir + "/confirm/interface_power", val)
    def set_confirm_unapplied(self, val):
        self.conf.set_bool(self.conf_dir + "/confirm/unapplied_dev", val)
    def set_confirm_delstorage(self, val):
        self.conf.set_bool(self.conf_dir + "/confirm/delete_storage", val)

    def on_confirm_forcepoweroff_changed(self, cb, userdata=None):
        return self.conf.notify_add(self.conf_dir + "/confirm/forcepoweroff", cb, userdata)
    def on_confirm_poweroff_changed(self, cb, userdata=None):
        return self.conf.notify_add(self.conf_dir + "/confirm/poweroff", cb, userdata)
    def on_confirm_pause_changed(self, cb, userdata=None):
        return self.conf.notify_add(self.conf_dir + "/confirm/pause", cb, userdata)
    def on_confirm_removedev_changed(self, cb, userdata=None):
        return self.conf.notify_add(self.conf_dir + "/confirm/removedev", cb, userdata)
    def on_confirm_interface_changed(self, cb, userdata=None):
        return self.conf.notify_add(self.conf_dir + "/confirm/interface_power", cb, userdata)
    def on_confirm_unapplied_changed(self, cb, userdata=None):
        return self.conf.notify_add(self.conf_dir + "/confirm/unapplied_dev", cb, userdata)
    def on_confirm_delstorage_changed(self, cb, userdata=None):
        return self.conf.notify_add(self.conf_dir + "/confirm/delete_storage", cb, userdata)


    # System tray visibility
    def on_view_system_tray_changed(self, cb, userdata=None):
        return self.conf.notify_add(self.conf_dir + "/system-tray", cb, userdata)
    def get_view_system_tray(self):
        return self.conf.get_bool(self.conf_dir + "/system-tray")
    def set_view_system_tray(self, val):
        self.conf.set_bool(self.conf_dir + "/system-tray", val)


    # Stats history and interval length
    def get_stats_update_interval(self):
        interval = self.conf.get_int(self.conf_dir + "/stats/update-interval")
        if interval < 1:
            return 1
        return interval
    def get_stats_history_length(self):
        history = self.conf.get_int(self.conf_dir + "/stats/history-length")
        if history < 10:
            return 10
        return history

    def set_stats_update_interval(self, interval):
        self.conf.set_int(self.conf_dir + "/stats/update-interval", interval)
    def set_stats_history_length(self, length):
        self.conf.set_int(self.conf_dir + "/stats/history-length", length)

    def on_stats_update_interval_changed(self, cb, userdata=None):
        return self.conf.notify_add(self.conf_dir + "/stats/update-interval", cb, userdata)
    def on_stats_history_length_changed(self, cb, userdata=None):
        return self.conf.notify_add(self.conf_dir + "/stats/history-length", cb, userdata)


    # Disable/Enable different stats polling
    def get_stats_enable_disk_poll(self):
        return self.conf.get_bool(self.conf_dir + "/stats/enable-disk-poll")
    def get_stats_enable_net_poll(self):
        return self.conf.get_bool(self.conf_dir + "/stats/enable-net-poll")

    def set_stats_enable_disk_poll(self, val):
        self.conf.set_bool(self.conf_dir + "/stats/enable-disk-poll", val)
    def set_stats_enable_net_poll(self, val):
        self.conf.set_bool(self.conf_dir + "/stats/enable-net-poll", val)

    def on_stats_enable_disk_poll_changed(self, cb, userdata=None):
        return self.conf.notify_add(self.conf_dir + "/stats/enable-disk-poll",
                                    cb, userdata)
    def on_stats_enable_net_poll_changed(self, cb, userdata=None):
        return self.conf.notify_add(self.conf_dir + "/stats/enable-net-poll",
                                    cb, userdata)

    # VM Console preferences
    def on_console_accels_changed(self, cb, userdata=None):
        return self.conf.notify_add(self.conf_dir + "/console/enable-accels", cb, userdata)
    def get_console_accels(self):
        console_pref = self.conf.get_bool(self.conf_dir +
                                          "/console/enable-accels")
        if console_pref is None:
            console_pref = False
        return console_pref
    def set_console_accels(self, pref):
        self.conf.set_bool(self.conf_dir + "/console/enable-accels", pref)

    def on_console_scaling_changed(self, cb, userdata=None):
        return self.conf.notify_add(self.conf_dir + "/console/scaling", cb, userdata)
    def get_console_scaling(self):
        ret = self.conf.get(self.conf_dir + "/console/scaling")
        if ret is not None:
            ret = ret.get_int()
        return ret
    def set_console_scaling(self, pref):
        self.conf.set_int(self.conf_dir + "/console/scaling", pref)

    # Show VM details toolbar
    def get_details_show_toolbar(self):
        res = self.conf.get_bool(self.conf_dir + "/details/show-toolbar")
        if res is None:
            res = True
        return res
    def set_details_show_toolbar(self, state):
        self.conf.set_bool(self.conf_dir + "/details/show-toolbar", state)

    # VM details default size
    def get_details_window_size(self):
        w = self.conf.get_int(self.conf_dir + "/details/window_width")
        h = self.conf.get_int(self.conf_dir + "/details/window_height")
        return (w, h)
    def set_details_window_size(self, w, h):
        self.conf.set_int(self.conf_dir + "/details/window_width", w)
        self.conf.set_int(self.conf_dir + "/details/window_height", h)

    # Create sound device for default guest
    def get_local_sound(self):
        return self.conf.get_bool(self.conf_dir + "/new-vm/local-sound")
    def get_remote_sound(self):
        return self.conf.get_bool(self.conf_dir + "/new-vm/remote-sound")

    def set_local_sound(self, state):
        self.conf.set_bool(self.conf_dir + "/new-vm/local-sound", state)
    def set_remote_sound(self, state):
        self.conf.set_bool(self.conf_dir + "/new-vm/remote-sound", state)

    def on_sound_local_changed(self, cb, data=None):
        return self.conf.notify_add(self.conf_dir + "/new-vm/local-sound", cb, data)
    def on_sound_remote_changed(self, cb, data=None):
        return self.conf.notify_add(self.conf_dir + "/new-vm/remote-sound", cb, data)

    def get_graphics_type(self):
        ret = self.conf.get_string(self.conf_dir + "/new-vm/graphics_type")
        if ret == "system":
            return self.default_graphics_from_config
        if ret not in ["vnc", "spice"]:
            return "vnc"
        return ret
    def set_graphics_type(self, gtype):
        self.conf.set_string(self.conf_dir + "/new-vm/graphics_type",
                             gtype.lower())
    def on_graphics_type_changed(self, cb, data=None):
        return self.conf.notify_add(self.conf_dir + "/new-vm/graphics_type",
                                    cb, data)

    def get_storage_format(self):
        ret = self.conf.get_string(self.conf_dir + "/new-vm/storage-format")
        if ret not in ["default", "raw", "qcow2"]:
            return "default"
        return ret
    def set_storage_format(self, typ):
        self.conf.set_string(self.conf_dir + "/new-vm/storage-format",
                             typ.lower())
    def on_storage_format_changed(self, cb, data=None):
        return self.conf.notify_add(self.conf_dir + "/new-vm/storage-format",
                                    cb, data)


    # URL/Media path history
    def _url_add_helper(self, gconf_path, url):
        urls = self.get_string_list(gconf_path)
        if urls is None:
            urls = []

        if urls.count(url) == 0 and len(url) > 0 and not url.isspace():
            # The url isn't already in the list, so add it
            urls.insert(0, url)
            length = self.get_url_list_length()
            if len(urls) > length:
                del urls[len(urls) - 1]
            self.set_string_list(gconf_path, urls)

    def add_media_url(self, url):
        self._url_add_helper(self.conf_dir + "/urls/media", url)
    def add_kickstart_url(self, url):
        self._url_add_helper(self.conf_dir + "/urls/kickstart", url)
    def add_iso_path(self, path):
        self._url_add_helper(self.conf_dir + "/urls/local_media", path)

    def get_media_urls(self):
        return self.get_string_list(self.conf_dir + "/urls/media")

    def get_kickstart_urls(self):
        return self.get_string_list(self.conf_dir + "/urls/kickstart")

    def get_iso_paths(self):
        return self.get_string_list(self.conf_dir + "/urls/local_media")

    def get_url_list_length(self):
        length = self.conf.get_int(self.conf_dir + "/urls/url-list-length")
        if length < 5:
            return 5
        return length
    def set_url_list_length(self, length):
        self.conf.set_int(self.conf_dir + "/urls/url-list-length", length)

    # Whether to ask about fixing path permissions
    def add_perms_fix_ignore(self, pathlist):
        current_list = self.get_perms_fix_ignore() or []
        for path in pathlist:
            if path in current_list:
                continue
            current_list.append(path)
        self.set_string_list(self.conf_dir + "/paths/perms_fix_ignore",
                             current_list)
    def get_perms_fix_ignore(self):
        return self.get_string_list(self.conf_dir + "/paths/perms_fix_ignore")


    # Manager view connection list
    def add_conn(self, uri):
        if self.test_first_run:
            return

        uris = self.get_string_list(self.conf_dir + "/connections/uris")
        if uris is None:
            uris = []

        if uris.count(uri) == 0:
            uris.insert(len(uris) - 1, uri)
            self.set_string_list(self.conf_dir + "/connections/uris", uris)
    def remove_conn(self, uri):
        uris = self.get_string_list(self.conf_dir + "/connections/uris")

        if uris is None:
            return

        if uris.count(uri) != 0:
            uris.remove(uri)
            self.set_string_list(self.conf_dir + "/connections/uris", uris)

        if self.get_conn_autoconnect(uri):
            uris = self.get_string_list(self.conf_dir +
                                             "/connections/autoconnect")
            uris.remove(uri)
            self.set_string_list(self.conf_dir + "/connections/autoconnect",
                                 uris)

    def get_conn_uris(self):
        if self.test_first_run:
            return []
        return self.get_string_list(self.conf_dir + "/connections/uris")

    # Manager default window size
    def get_manager_window_size(self):
        w = self.conf.get_int(self.conf_dir + "/manager_window_width")
        h = self.conf.get_int(self.conf_dir + "/manager_window_height")
        return (w, h)
    def set_manager_window_size(self, w, h):
        self.conf.set_int(self.conf_dir + "/manager_window_width", w)
        self.conf.set_int(self.conf_dir + "/manager_window_height", h)

    # URI autoconnect
    def get_conn_autoconnect(self, uri):
        uris = self.get_string_list(self.conf_dir + "/connections/autoconnect")
        return ((uris is not None) and (uri in uris))

    def set_conn_autoconnect(self, uri, val):
        if self.test_first_run:
            return

        uris = self.get_string_list(self.conf_dir + "/connections/autoconnect")
        if uris is None:
            uris = []
        if not val and uri in uris:
            uris.remove(uri)
        elif val and uri not in uris:
            uris.append(uri)

        self.set_string_list(self.conf_dir + "/connections/autoconnect",
                             uris)


    # Default directory location dealings
    def _get_default_dir_key(self, typ):
        if (typ == self.CONFIG_DIR_ISO_MEDIA or
            typ == self.CONFIG_DIR_FLOPPY_MEDIA):
            return "media"
        return typ

    def get_default_directory(self, conn, _type):
        if not _type:
            logging.error("Unknown type '%s' for get_default_directory", _type)
            return

        key = self._get_default_dir_key(_type)
        try:
            path = self.conf.get_value(self.conf_dir +
                                       "/paths/default-%s-path" % key)
        except:
            path = None

        if not path:
            if (_type == self.CONFIG_DIR_IMAGE or
                _type == self.CONFIG_DIR_ISO_MEDIA or
                _type == self.CONFIG_DIR_FLOPPY_MEDIA):
                path = self.get_default_image_dir(conn)
            if (_type == self.CONFIG_DIR_SAVE or
                _type == self.CONFIG_DIR_RESTORE):
                path = self.get_default_save_dir(conn)

        logging.debug("get_default_directory(%s): returning %s", _type, path)
        return path

    def set_default_directory(self, folder, _type):
        if not _type:
            logging.error("Unknown type for set_default_directory")
            return

        logging.debug("set_default_directory(%s): saving %s", _type, folder)
        self.conf.set_string(self.conf_dir + "/paths/default-%s-path" % _type,
                             folder)

    def get_default_image_dir(self, conn):
        if conn.is_xen():
            return self.DEFAULT_XEN_IMAGE_DIR

        if (conn.is_qemu_session() or
            not os.access(self.DEFAULT_VIRT_IMAGE_DIR, os.W_OK)):
            return os.getcwd()

        # Just return the default dir since the intention is that it
        # is a managed pool and the user will be able to install to it.
        return self.DEFAULT_VIRT_IMAGE_DIR

    def get_default_save_dir(self, conn):
        if conn.is_xen():
            return self.DEFAULT_XEN_SAVE_DIR
        elif os.access(self.DEFAULT_VIRT_SAVE_DIR, os.W_OK):
            return self.DEFAULT_VIRT_SAVE_DIR
        else:
            return os.getcwd()


    # Keyring / VNC password dealings
    def get_secret_name(self, vm):
        return "vm-console-" + vm.get_uuid()

    def has_keyring(self):
        if self.keyring is None:
            self.keyring = vmmKeyring()
        return self.keyring.is_available()


    def get_console_password(self, vm):
        keyid = self.conf.get_int(self.conf_dir +
                                "/console/passwords/" + vm.get_uuid())
        username = self.conf.get_string(self.conf_dir +
                                        "/console/usernames/" + vm.get_uuid())

        if keyid is None or not self.has_keyring():
            return ("", "")

        secret = self.keyring.get_secret(keyid)
        if secret is None or secret.get_name() != self.get_secret_name(vm):
            return ("", "")

        if (secret.attributes.get("hvuri", None) != vm.conn.get_uri() or
            secret.attributes.get("uuid", None) != vm.get_uuid()):
            return ("", "")

        return (secret.get_secret(), username or "")

    def set_console_password(self, vm, password, username=""):
        if not self.has_keyring():
            return

        # Nb, we don't bother to check if there is an existing
        # secret, because gnome-keyring auto-replaces an existing
        # one if the attributes match - which they will since UUID
        # is our unique key

        secret = vmmSecret(self.get_secret_name(vm), password,
                           {"uuid" : vm.get_uuid(),
                            "hvuri": vm.conn.get_uri()})
        keyid = self.keyring.add_secret(secret)
        if keyid is None:
            return

        self.conf.set_int(self.conf_dir +
                          "/console/passwords/" + vm.get_uuid(), keyid)
        self.conf.set_string(self.conf_dir +
                             "/console/usernames/" + vm.get_uuid(), username)

#!/usr/bin/python


class TunnelMode(object):

    def __init__(self, name):
        self.name = name

    def init_overlay_data(self, overlayid,
                          overlay_name, tenantid, overlay_info):
        pass

    def init_tunnel_mode(self, deviceid, tenantid, overlay_info):
        pass

    def init_overlay(self, overlayid, overlay_name,
                     overlay_type, tenantid, deviceid, overlay_info):
        pass

    def add_slice_to_overlay(self, overlayid, overlay_name,
                             deviceid, interface_name, tenantid, overlay_info):
        pass

    def create_tunnel(self, overlayid, overlay_name, overlay_type,
                      l_slice, r_slice, tenantid, overlay_info):
        pass

    def destroy_overlay_data(self, overlayid,
                             overlay_name, tenantid, overlay_info,
                             ignore_errors=False):
        pass

    def destroy_tunnel_mode(self, deviceid, tenantid, overlay_info,
                            ignore_errors=False):
        pass

    def destroy_overlay(self, overlayid, overlay_name,
                        overlay_type, tenantid, deviceid, overlay_info,
                        ignore_errors=False):
        pass

    def remove_slice_from_overlay(self, overlayid, overlay_name,
                                  deviceid, interface_name,
                                  tenantid, overlay_info,
                                  ignore_errors=False):
        pass

    def remove_tunnel(self, overlayid, overlay_name, overlay_type,
                      l_slice, r_slice, tenantid, overlay_info,
                      ignore_errors=False):
        pass

    def get_overlays(self):
        raise NotImplementedError

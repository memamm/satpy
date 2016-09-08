#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright (c) 2012, 2013, 2014 Martin Raspaud

# Author(s):

#   Martin Raspaud <martin.raspaud@smhi.se>

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Reader for eps level 1b data. Uses xml files as a format description.
"""

import logging
import os
from datetime import datetime

import numpy as np
from pyresample.geometry import SwathDefinition

from satpy.config import CONFIG_PATH
from satpy.projectable import Projectable
from satpy.readers import ConfigBasedReader, GenericFileReader
from satpy.readers.file_handlers import BaseFileHandler
from satpy.satin.xmlformat import XMLFormat

LOG = logging.getLogger(__name__)

try:
    from numexpr import evaluate
except ImportError:
    from numpy import log
    evaluate = eval

C1 = 1.191062e-05  # mW/(m2*sr*cm-4)
C2 = 1.4387863  # K/cm-1


def radiance_to_bt(arr, wc_, a__, b__):
    """Convert to BT.
    """
    return evaluate("a__ + b__ * (C2 * wc_ / "
                    "(log(1 + (C1 * (wc_ ** 3) / arr))))")


def radiance_to_refl(arr, solar_flux):
    """Convert to reflectances.
    """
    return arr * np.pi * 100.0 / solar_flux


def read_raw(filename):
    """Read *filename* without scaling it afterwards.
    """

    form = XMLFormat(os.path.join(CONFIG_PATH, "eps_avhrrl1b_6.5.xml"))

    grh_dtype = np.dtype([("record_class", "|i1"),
                          ("INSTRUMENT_GROUP", "|i1"),
                          ("RECORD_SUBCLASS", "|i1"),
                          ("RECORD_SUBCLASS_VERSION", "|i1"),
                          ("RECORD_SIZE", ">u4"),
                          ("RECORD_START_TIME", "S6"),
                          ("RECORD_STOP_TIME", "S6")])

    record_class = ["Reserved", "mphr", "sphr",
                    "ipr", "geadr", "giadr",
                    "veadr", "viadr", "mdr"]

    records = []

    with open(filename, "rb") as fdes:
        while True:
            grh = np.fromfile(fdes, grh_dtype, 1)
            if not grh:
                break
            rec_class = record_class[grh["record_class"]]
            sub_class = grh["RECORD_SUBCLASS"][0]
            offset = fdes.tell()
            try:
                record = np.memmap(fdes,
                                   form.dtype((rec_class,
                                               sub_class)),
                                   mode='r',
                                   offset=offset,
                                   shape=(1, ))
            except KeyError:
                fdes.seek(grh["RECORD_SIZE"] - 20, 1)
            else:
                fdes.seek(offset + grh["RECORD_SIZE"] - 20, 0)
                records.append((rec_class, record, sub_class))

    return records, form


class EPSAVHRRFile(BaseFileHandler):
    """Eps level 1b reader for AVHRR data.
    """
    spacecrafts = {"M01": "Metop-B",
                   "M02": "Metop-A",
                   "M03": "Metop-C", }

    sensors = {"AVHR": "avhrr/3"}

    def __init__(self, filename, filename_info, filetype_info):
        super(EPSAVHRRFile, self).__init__(
            filename, filename_info, filetype_info)
        self.records, self.form = read_raw(filename)
        self.mdrs = [record[1]
                     for record in self.records
                     if record[0] == "mdr"]
        self.scanlines = len(self.mdrs)
        self.sections = {("mdr", 2): np.concatenate(self.mdrs)}
        for record in self.records:
            if record[0] == "mdr":
                continue
            if (record[0], record[2]) in self.sections:
                raise ValueError("Too many " + str((record[0], record[2])))
            else:
                self.sections[(record[0], record[2])] = record[1]
        self.lons, self.lats = None, None
        self.area = None
        self.three_a_mask, self.three_b_mask = None, None

    def __getitem__(self, key):
        for altkey in self.form.scales.keys():
            try:
                try:
                    return (self.sections[altkey][key]
                            * self.form.scales[altkey][key])
                except TypeError:
                    val = self.sections[altkey][key][0].split("=")[1]
                    try:
                        return float(val) * self.form.scales[altkey][key]
                    except ValueError:
                        return val.strip()
            except (KeyError, ValueError):
                continue
        raise KeyError("No matching value for " + str(key))

    def keys(self):
        """List of reader's keys.
        """
        keys = []
        for val in self.form.scales.values():
            keys += val.dtype.fields.keys()
        return keys

    def get_full_lonlats(self):
        """Get the interpolated lons/lats.
        """
        lats = np.hstack((self["EARTH_LOCATION_FIRST"][:, [0]],
                          self["EARTH_LOCATIONS"][:, :, 0],
                          self["EARTH_LOCATION_LAST"][:, [0]]))

        lons = np.hstack((self["EARTH_LOCATION_FIRST"][:, [1]],
                          self["EARTH_LOCATIONS"][:, :, 1],
                          self["EARTH_LOCATION_LAST"][:, [1]]))

        nav_sample_rate = self["NAV_SAMPLE_RATE"]
        earth_views_per_scanline = self["EARTH_VIEWS_PER_SCANLINE"]
        if nav_sample_rate == 20 and earth_views_per_scanline == 2048:
            from geotiepoints import metop20kmto1km
            self.lons, self.lats = metop20kmto1km(lons, lats)
        else:
            raise NotImplementedError("Lon/lat expansion not implemented for " +
                                      "sample rate = " + str(nav_sample_rate) +
                                      " and earth views = " +
                                      str(earth_views_per_scanline))
        return self.lons, self.lats

    def get_lonlat(self, row, col):
        """Get lons/lats for given indices. WARNING: if the lon/lats were not
        expanded, this will refer to the tiepoint data.
        """
        if self.lons is None or self.lats is None:
            self.lats = np.hstack((self["EARTH_LOCATION_FIRST"][:, [0]],
                                   self["EARTH_LOCATIONS"][:, :, 0],
                                   self["EARTH_LOCATION_LAST"][:, [0]]))

            self.lons = np.hstack((self["EARTH_LOCATION_FIRST"][:, [1]],
                                   self["EARTH_LOCATIONS"][:, :, 1],
                                   self["EARTH_LOCATION_LAST"][:, [1]]))
        return self.lons[row, col], self.lats[row, col]

    def get_dataset(self, key, info):
        """Get calibrated channel data.
        *calib_type* = 0: Counts
        *calib_type* = 1: Reflectances and brightness temperatures
        *calib_type* = 2: Radiances
        """
        if key.calibration == 'counts':
            raise ValueError('calibrate=0 is not supported! ' +
                             'This reader cannot return counts')
        elif key.calibration not in ['reflectance', 'brightness_temperature', 'radiance']:
            raise ValueError('calibration type' + str(key.calibration) +
                             'is not supported!')

        if key.name in ['3A', '3a'] and self.three_a_mask is None:
            self.three_a_mask = (
                (self["FRAME_INDICATOR"] & 2 ** 16) != 2 ** 16)

        if key.name in ['3B', '3b'] and self.three_b_mask is None:
            self.three_b_mask = ((self["FRAME_INDICATOR"] & 2 ** 16) != 0)

        if key.name not in ["1", "2", "3a", "3A", "3b", "3B", "4", "5"]:
            LOG.info("Can't load channel in eps_l1b: " + str(key.name))
            return

        if key.name == "1":
            if key.calibration == 'reflectance':
                array = np.ma.array(
                    radiance_to_refl(self["SCENE_RADIANCES"][:, 0, :],
                                     self["CH1_SOLAR_FILTERED_IRRADIANCE"]))
            else:
                array = np.ma.array(
                    self["SCENE_RADIANCES"][:, 0, :])

        if key.name == "2":
            if key.calibration == 'reflectance':
                array = np.ma.array(
                    radiance_to_refl(self["SCENE_RADIANCES"][:, 1, :],
                                     self["CH1_SOLAR_FILTERED_IRRADIANCE"]))
            else:
                array = np.ma.array(
                    self["SCENE_RADIANCES"][:, 1, :])

        if key.name.lower() == "3a":
            if key.calibration == 'reflectance':
                array = np.ma.array(
                    radiance_to_refl(self["SCENE_RADIANCES"][:, 2, :],
                                     self["CH2_SOLAR_FILTERED_IRRADIANCE"]))
            else:
                array = np.ma.array(self["SCENE_RADIANCES"][:, 2, :])

            mask = np.empty(array.shape, dtype=bool)
            mask[:, :] = self.three_a_mask[:, np.newaxis]
            array = np.ma.array(array, mask=mask, copy=False)
        if key.name.lower() == "3b":
            if key.calibration == 'brightness_temperature':
                array = np.array(
                    radiance_to_bt(self["SCENE_RADIANCES"][:, 2, :],
                                   self["CH3B_CENTRAL_WAVENUMBER"],
                                   self["CH3B_CONSTANT1"],
                                   self["CH3B_CONSTANT2_SLOPE"]))
            else:
                array = self["SCENE_RADIANCES"][:, 2, :]
            mask = np.empty(array.shape, dtype=bool)
            mask[:, :] = self.three_b_mask[:, np.newaxis]
            array = np.ma.array(array, mask=mask, copy=False)
        if key.name == "4":
            if key.calibration == 'brightness_temperature':
                array = np.ma.array(
                    radiance_to_bt(self["SCENE_RADIANCES"][:, 3, :],
                                   self["CH4_CENTRAL_WAVENUMBER"],
                                   self["CH4_CONSTANT1"],
                                   self["CH4_CONSTANT2_SLOPE"]))
            else:
                array = np.ma.array(
                    self["SCENE_RADIANCES"][:, 3, :])

        if key.name == "5":
            if key.calibration == 'brightness_temperature':
                array = np.ma.array(
                    radiance_to_bt(self["SCENE_RADIANCES"][:, 4, :],
                                   self["CH5_CENTRAL_WAVENUMBER"],
                                   self["CH5_CONSTANT1"],
                                   self["CH5_CONSTANT2_SLOPE"]))
            else:
                array = np.ma.array(self["SCENE_RADIANCES"][:, 4, :])

        proj = Projectable(array, mask=array.mask, id=key)
        proj.info['area'] = self.get_area()
        return proj

    def get_area(self):
        if self.area is None:
            if self.lons is None or self.lats is None:
                self.lons, self.lats = self.get_full_lonlats()
            self.area = SwathDefinition(self.lons, self.lats)
            self.area.name = '_'.join([self.platform_name, str(self.start_time()),
                                       str(self.end_time())])
        return self.area

    @property
    def platform_name(self):
        return self.spacecrafts[self["SPACECRAFT_ID"]]

    @property
    def sensor_name(self):
        return self.sensors[self["INSTRUMENT_ID"]]

    def start_time(self):
        return datetime.strptime(self["SENSING_START"], "%Y%m%d%H%M%SZ")

    def end_time(self):
        return datetime.strptime(self["SENSING_END"], "%Y%m%d%H%M%SZ")


class AVHRREPSL1BFileReader(GenericFileReader):

    spacecrafts = {"M01": "Metop-B",
                   "M02": "Metop-A",
                   "M03": "Metop-C", }

    sensors = {"AVHR": "avhrr/3"}

    def create_file_handle(self, filename, **kwargs):
        self.records, self.form = read_raw(filename)
        self.mdrs = [record[1]
                     for record in self.records
                     if record[0] == "mdr"]
        self.scanlines = len(self.mdrs)
        self.sections = {("mdr", 2): np.concatenate(self.mdrs)}
        for record in self.records:
            if record[0] == "mdr":
                continue
            if (record[0], record[2]) in self.sections:
                raise ValueError("Too many " + str((record[0], record[2])))
            else:
                self.sections[(record[0], record[2])] = record[1]
        self.lons, self.lats = None, None

        self.cache = {}
        # no single file handle for these files, so just return None and
        # override any parent class methods
        return filename, None

    def get_swath_data(self, item, data_out=None, mask_out=None):
        """
        :param item: usually a channel name
        :return:
        """
        var_info = self.file_keys[item]
        if "geo_index" in var_info.kwargs:
            variable_names = var_info.variable_name.split(",")
            geo_index = int(var_info.kwargs["geo_index"])
            if data_out is not None:
                data_out[:] = np.hstack((self[variable_names[0]][:, [geo_index]],
                                         self[variable_names[1]][
                                             :, :, geo_index],
                                         self[variable_names[2]][:, [geo_index]]))
                return
            else:
                return np.hstack((self[variable_names[0]][:, [geo_index]],
                                  self[variable_names[1]][:, :, geo_index],
                                  self[variable_names[2]][:, [geo_index]]))
        elif item.startswith("radiance") or item.startswith("reflectance") or item.startswith("bt"):
            return self._get_channel(item, data_out, mask_out)
        else:
            raise ValueError("Unknown file key: %s" % (item,))

    def _get_channel(self, item, data_out=None, mask_out=None):
        """Get calibrated channel data.
        """
        var_info = self.file_keys[item]
        var_name = var_info.variable_name

        data_arr = self[var_name]
        radiance_shape = data_arr.shape
        if data_out is None:
            data_out = np.ma.empty(
                (radiance_shape[0], radiance_shape[2]), dtype=data_arr.dtype)
        if data_out is None:
            mask_out = np.ma.empty(
                (radiance_shape[0], radiance_shape[2]), dtype=bool)

        if "frame_indicator" in var_info.kwargs:
            frame_indicator = int(var_info.kwargs["frame_indicator"])
            frames = (self[self.file_keys[
                      "frame_indicator"].variable_name] & 2 ** 16) == frame_indicator
            data_out[frames, :] = data_arr[frames, int(
                var_info.kwargs.get("band_index", 0)), :]
            mask_out[~frames, :] = True
            mask_out[frames, :] = False
        else:
            data_out[:] = data_arr[
                :, int(var_info.kwargs.get("band_index", 0)), :]
            mask_out[:] = False

        # FIXME: Temporary, calibrate data here
        calib_type = var_info.kwargs.get("calib_type")
        if calib_type == "bt":
            wl = self[self.file_keys[var_info.kwargs["cw_key"]].variable_name]
            chan_const = self[self.file_keys[
                var_info.kwargs["channel_constant_key"]].variable_name]
            slope_const = self[self.file_keys[
                var_info.kwargs["slope_constant_key"]].variable_name]
            data_out[:] = radiance_to_bt(data_out,
                                         wl,
                                         chan_const,
                                         slope_const)
        elif calib_type == "reflectance":
            sfi = self[self.file_keys[var_info.kwargs[
                "solar_irradiance"]].variable_name]
            data_out[:] = radiance_to_refl(data_out, sfi)

        # Simple unit conversion
        file_units = self.get_file_units(item)
        output_units = getattr(var_info, "units", file_units)
        if file_units == "1" and output_units == "%":
            data_out[:] *= 100.0

        return data_out

    def get_shape(self, item):
        if item in ["longitude", "latitude"]:
            return int(self["TOTAL_MDR"]), int(max(self["NUM_NAVIGATION_POINTS"]) + 2)
        else:
            return int(self["TOTAL_MDR"]), int(self[self.file_keys["views_per_scanline"].variable_name])

    @property
    def platform_name(self):
        return self.spacecrafts[self["SPACECRAFT_ID"]]

    @property
    def sensor_name(self):
        return self.sensors[self["INSTRUMENT_ID"]]

    def get_file_units(self, item):
        return getattr(self.file_keys[item], "file_units", None)

    @property
    def geofilename(self):
        return self.filename

    @property
    def ring_lonlats(self):
        raise NotImplementedError

    @property
    def begin_orbit_number(self):
        return self["ORBIT_START"]

    @property
    def end_orbit_number(self):
        return self["ORBIT_END"]

    def _get_start_time(self):
        return datetime.strptime(self[self.file_keys["start_time"].variable_name], "%Y%m%d%H%M%SZ")

    def _get_end_time(self):
        return datetime.strptime(self[self.file_keys["end_time"].variable_name], "%Y%m%d%H%M%SZ")


if __name__ == '__main__':
    def norm255(a__):
        """normalize array to uint8.
        """
        arr = a__ * 1.0
        arr = (arr - arr.min()) * 255.0 / (arr.max() - arr.min())
        return arr.astype(np.uint8)

    def show(a__):
        """show array.
        """
        from PIL import Image
        Image.fromarray(norm255(a__), "L").show()

    import sys
    res = read_raw(sys.argv[1])
#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) 2016.

# Author(s):

#   Martin Raspaud <martin.raspaud@smhi.se>

# This file is part of satpy.

# satpy is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.

# satpy is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE.  See the GNU General Public License for more details.

# You should have received a copy of the GNU General Public License along with
# satpy.  If not, see <http://www.gnu.org/licenses/>.

# New stuff

import glob
import logging
import numbers
import os
from fnmatch import fnmatch

import six
import yaml

from satpy.readers import DatasetID
from trollsift.parser import globify, parse

LOG = logging.getLogger(__name__)


class YAMLBasedReader(object):

    def __init__(self, config_files):
        self.config = {}
        self.config_files = config_files
        for config_file in config_files:
            with open(config_file) as fd:
                self.config.update(yaml.load(fd))
        self.datasets = self.config['datasets']
        self.info = self.config['reader']
        self.name = self.info['name']
        self.sensor_names = self.info['sensors']

        self.info['filenames'] = []
        self.file_patterns = []
        for file_type in self.config['file_types'].values():
            self.file_patterns.extend(file_type['file_patterns'])
        self.ids = {}
        self.get_dataset_ids()

    def select_files(self, base_dir=None, filenames=None, sensor=None, reader_name=None):

        if reader_name is not None and self.info['name'] != reader_name:
            return filenames, []

        if isinstance(sensor, (str, six.text_type)):
            sensor_set = set([sensor])
        elif sensor is not None:
            sensor_set = set(sensor)
        else:
            sensor_set = set()

        if sensor is not None and not (set(self.info.get("sensors")) & sensor_set):
            return filenames, []

        file_set = set()
        if filenames:
            file_set |= set(filenames)

        if filenames is None:
            self.info["filenames"] = self.find_filenames(base_dir)
        else:
            self.info["filenames"] = self.match_filenames(filenames, base_dir)
        if not self.info["filenames"]:
            LOG.warning("No filenames found for reader: %s", self.name)
        file_set -= set(self.info['filenames'])
        LOG.debug("Assigned to %s: %s", self.info['name'], self.info['filenames'])
        return file_set, self.info['filenames']

    def match_filenames(self, filenames, base_dir=None):
        result = []
        for file_pattern in self.file_patterns:
            if base_dir is not None:
                file_pattern = os.path.join(base_dir, file_pattern)
            pattern = globify(file_pattern)
            if not filenames:
                return result
            for filename in list(filenames):
                if fnmatch(os.path.basename(filename), os.path.basename(pattern)):
                    result.append(filename)
                    filenames.remove(filename)
        return result

    def find_filenames(self, directory, file_patterns=None):
        if file_patterns is None:
            file_patterns = self.file_patterns
            # file_patterns.extend(item['file_patterns'] for item in self.config['file_types'])
        filelist = []
        for pattern in file_patterns:
            filelist.extend(glob.iglob(os.path.join(directory, globify(pattern))))
        return filelist

    def get_dataset_ids(self):
        """Get the dataset ids from the config."""
        ids = []
        for dskey, dataset in self.datasets.items():
            nb_ids = 1
            for key, val in dataset.items():
                if not key.endswith('range') and isinstance(val, (list, tuple, set)):
                    nb_ids = len(val)
                    break
            for idx in range(nb_ids):
                kwargs = {'name': dataset.get('name'),
                          'wavelength': tuple(dataset.get('wavelength_range'))}
                for key in ['resolution', 'calibration', 'polarization']:
                    kwargs[key] = dataset.get(key)
                    if isinstance(kwargs[key], (list, tuple, set)):
                        kwargs[key] = kwargs[key][idx]
                dsid = DatasetID(**kwargs)
                ids.append(dsid)
                self.ids[dsid] = dskey, dataset
        return ids

    def load(self, dataset_keys, area=None, start_time=None, end_time=None, **kwargs):
        loaded_filenames = {}
        datasets = {}
        for dataset_key in dataset_keys:
            dsid = self.get_dataset_key(dataset_key)
            filetype = self.ids[dsid][1]['file_type']
            types = {filetype: self.config['file_types'][filetype]['file_patterns']}
            if 'navigation' in self.ids[dsid][1]:
                nav_type = self.ids[dsid][1]['navigation']
                nav_patterns = self.config['file_types'][nav_type]['file_patterns']
                types[nav_type] = nav_patterns

            # filenames = self.find_filenames(base_directory, self.config['file_types'][filetype]['file_patterns'])
            filenames = self.info['filenames']
            res = match_file_names_and_types(filenames, types)
            for filename, filename_info in res[filetype]:
                if filename in loaded_filenames:
                    fhd = loaded_filenames[filename]
                else:
                    fhd = self.config['file_types'][filetype]['file_reader'](filename, filename_info)
                    if 'navigation' in self.ids[dsid][1]:
                        match = True
                        for nav_name, nav_info in res[nav_type]:
                            # This might be too strict for some data types/filenames
                            shared_items = set(nav_info.items()) & set(filename_info.items())
                            if len(shared_items) == len(nav_info):
                                break
                        else:
                            LOG.warning("Can't find a navigation file for %s", filename)
                            match = False
                        if match:
                            if nav_name in loaded_filenames:
                                fhd.navigation_reader = loaded_filenames[nav_name]
                            else:
                                fhd.navigation_reader = self.config['file_types'][nav_type]['file_reader'](nav_name)
                                loaded_filenames[nav_name] = fhd.navigation_reader
                    loaded_filenames[filename] = fhd
                datasets.setdefault(dsid, []).append(fhd)

        return multiload(datasets, area, start_time, end_time)

    def get_dataset_key(self, key, calibration=None, resolution=None, polarization=None, aslist=False):
        """Get the fully qualified dataset corresponding to *key*, either by name or centerwavelength.

        If `key` is a `DatasetID` object its name is searched if it exists, otherwise its wavelength is used.
        """
        # TODO This can be made simpler
        # get by wavelength
        if isinstance(key, numbers.Number):
            datasets = [ds for ds in self.ids if ds.wavelength and (ds.wavelength[0] <= key <= ds.wavelength[2])]
            datasets = sorted(datasets, key=lambda ch: abs(ch.wavelength[1] - key))
            if not datasets:
                raise KeyError("Can't find any projectable at %gum" % key)
        elif isinstance(key, DatasetID):
            if key.name is not None:
                datasets = self.get_dataset_key(key.name, aslist=True)
            elif key.wavelength is not None:
                datasets = self.get_dataset_key(key.wavelength, aslist=True)
            else:
                raise KeyError("Can't find any projectable '{}'".format(key))

            if calibration is None and key.calibration is not None:
                calibration = [key.calibration]
            if resolution is None and key.resolution is not None:
                resolution = [key.resolution]
            if polarization is None and key.polarization is not None:
                polarization = [key.polarization]
        # get by name
        else:
            datasets = [ds_id for ds_id in self.ids if ds_id.name == key]
            if not datasets:
                raise KeyError("Can't find any projectable called '{}'".format(key))
        # default calibration choices
        if calibration is None:
            calibration = ["brightness_temperature", "reflectance"]

        if resolution is not None:
            if not isinstance(resolution, (tuple, list, set)):
                resolution = [resolution]
            datasets = [ds_id for ds_id in datasets if ds_id.resolution in resolution]
        if calibration is not None:
            # order calibration from highest level to lowest level
            calibration = [x for x in ["brightness_temperature",
                                       "reflectance", "radiance", "counts"] if x in calibration]
            datasets = [ds_id for ds_id in datasets if ds_id.calibration is None or ds_id.calibration in calibration]
        if polarization is not None:
            datasets = [ds_id for ds_id in datasets if ds_id.polarization in polarization]

        if not datasets:
            raise KeyError("Can't find any projectable matching '{}'".format(str(key)))
        if aslist:
            return datasets
        else:
            return datasets[0]


def match_file_names_and_types(filenames, filetypes):
    res = {}
    for filename in filenames:
        for filetype, patterns in filetypes.items():
            for pattern in patterns:
                if fnmatch(os.path.basename(filename), globify(pattern)):
                    res.setdefault(filetype, []).append((filename, parse(pattern, os.path.basename(filename))))
                    break
            if filename in res:
                break
    return res


def my_vstack(arrays):
    return arrays[0]

# what about the metadata ?


def multiload(datasets, area, start_time, end_time):
    files = {}
    # select files to use
    trimmed_datasets = datasets.copy()
    first_start_time = None
    last_end_time = None
    for dataset, fhds in trimmed_datasets.items():
        for fhd in reversed(fhds):
            remove = False
            if start_time and fhd.start_time() < start_time:
                remove = True
            else:
                if first_start_time is None or first_start_time > fhd.start_time():
                    first_start_time = fhd.start_time()
            if end_time and fhd.end_time() > fhd.end_time():
                remove = True
            else:
                if last_end_time is None or last_end_time < fhd.end_time():
                    last_end_time = fhd.end_time()
            # TODO: area-based selection
            if remove:
                fhds.remove(fhd)
            else:
                files.setdefault(fhd, []).append(dataset)

    res = []
    for fhd, datasets in files.items():
        res.extend(fhd.load(datasets))

    # sort datasets by ds_id and time

    datasets = {}
    for dataset in res:
        datasets.setdefault(dataset.info['id'], []).append(dataset)

    # FIXME: stacks should end up being datasets.
    res = dict([(key, my_vstack(dss)) for key, dss in datasets.items()])

    # for dataset in res:
#        dataset.info['start_time'] = first_start_time
#        dataset.info['end_time'] = last_end_time

    # TODO: take care of the metadata

    return res


# what about file pattern and config ?
class SatFileHandler(object):

    def __init__(self, filename, filename_info):
        self.filename = filename
        self.navigation_reader = None
        self.filename_metadata = filename_info

    def get_shape(self, dataset_id):
        raise NotImplementedError

    def start_time(self):
        raise NotImplementedError

    def end_time(self):
        raise NotImplementedError


class GeoFileHandler(SatFileHandler):

    def get_area(self, resolution=None):
        raise NotImplementedError

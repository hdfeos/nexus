"""
Copyright (c) 2016 Jet Propulsion Laboratory,
California Institute of Technology.  All rights reserved
"""
from functools import wraps
import ConfigParser
import pkg_resources
from StringIO import StringIO

import numpy as np
import numpy.ma as ma

from dao.CassandraProxy import CassandraProxy
from dao.SolrProxy import SolrProxy
from model.nexusmodel import Tile, BBox, TileStats


def tile_data(default_fetch=True):
    def tile_data_decorator(func):
        @wraps(func)
        def fetch_data_for_func(*args, **kwargs):
            if ('fetch_data' not in kwargs and default_fetch == False) or (
                            'fetch_data' in kwargs and kwargs['fetch_data'] == False):
                return func(*args, **kwargs)
            else:
                solr_docs = func(*args, **kwargs)
                tiles = args[0]._solr_docs_to_tiles(*solr_docs)
                if len(tiles) > 0:
                    args[0].fetch_data_for_tiles(*tiles)
                return tiles

        return fetch_data_for_func

    return tile_data_decorator


class NexusTileService(object):
    def __init__(self, skipCassandra=False, skipSolr=False):
        self._config = ConfigParser.RawConfigParser()

        self._config.readfp(pkg_resources.resource_stream(__name__, "config/datastores.ini"), filename='datastores.ini')

        if not skipCassandra:
            self._cass = CassandraProxy(self._config)

        if not skipSolr:
            self._solr = SolrProxy(self._config)

    def get_dataseries_list(self):
        return self._solr.get_data_series_list()

    @tile_data()
    def find_tile_by_id(self, tile_id, **kwargs):
        return self._solr.find_tile_by_id(tile_id)

    def find_days_in_range_asc(self, min_lat, max_lat, min_lon, max_lon, dataset, start_time, end_time, **kwargs):
        return self._solr.find_days_in_range_asc(min_lat, max_lat, min_lon, max_lon, dataset, start_time, end_time,
                                                 **kwargs)

    @tile_data()
    def find_tile_by_bbox_and_most_recent_day_of_year(self, min_lat, max_lat, min_lon, max_lon, ds, day_of_year,
                                                      **kwargs):
        return self._solr.find_tile_by_bbox_and_most_recent_day_of_year(min_lat, max_lat, min_lon, max_lon, ds,
                                                                        day_of_year)

    @tile_data()
    def find_all_tiles_in_box_at_time(self, min_lat, max_lat, min_lon, max_lon, dataset, time, **kwargs):
        return self._solr.find_all_tiles_in_box_at_time(min_lat, max_lat, min_lon, max_lon, dataset, time, rows=5000,
                                                        **kwargs)

    @tile_data()
    def find_tiles_in_box(self, min_lat, max_lat, min_lon, max_lon, ds=None, start_time=0, end_time=-1, **kwargs):
        # Find chunks that fall in the given box in the Solr index
        return self._solr.find_all_tiles_in_box_sorttimeasc(min_lat, max_lat, min_lon, max_lon, ds, start_time,
                                                            end_time, **kwargs)

    @tile_data()
    def find_all_boundary_tiles_at_time(self, min_lat, max_lat, min_lon, max_lon, dataset, time, **kwargs):
        return self._solr.find_all_boundary_tiles_at_time(min_lat, max_lat, min_lon, max_lon, dataset, time, rows=5000,
                                                          **kwargs)

    def get_tiles_bounded_by_box(self, min_lat, max_lat, min_lon, max_lon, ds=None, start_time=0, end_time=-1,
                                 **kwargs):
        tiles = self.find_tiles_in_box(min_lat, max_lat, min_lon, max_lon, ds, start_time, end_time, **kwargs)
        tiles = self.mask_tiles_to_bbox(min_lat, max_lat, min_lon, max_lon, tiles)

        return tiles

    def get_tiles_bounded_by_box_at_time(self, min_lat, max_lat, min_lon, max_lon, dataset, time, **kwargs):
        tiles = self.find_all_tiles_in_box_at_time(min_lat, max_lat, min_lon, max_lon, dataset, time, **kwargs)
        tiles = self.mask_tiles_to_bbox(min_lat, max_lat, min_lon, max_lon, tiles)

        return tiles

    def get_boundary_tiles_at_time(self, min_lat, max_lat, min_lon, max_lon, dataset, time, **kwargs):
        tiles = self.find_all_boundary_tiles_at_time(min_lat, max_lat, min_lon, max_lon, dataset, time, **kwargs)
        tiles = self.mask_tiles_to_bbox(min_lat, max_lat, min_lon, max_lon, tiles)

        return tiles

    def get_stats_within_box_at_time(self, min_lat, max_lat, min_lon, max_lon, dataset, time, **kwargs):
        tiles = self._solr.find_all_tiles_within_box_at_time(min_lat, max_lat, min_lon, max_lon, dataset, time,
                                                             **kwargs)

        return tiles

    def mask_tiles_to_bbox(self, min_lat, max_lat, min_lon, max_lon, tiles):

        for tile in tiles:
            tile.latitudes = ma.masked_outside(tile.latitudes, min_lat, max_lat)
            tile.longitudes = ma.masked_outside(tile.longitudes, min_lon, max_lon)

            # Or together the masks of the individual arrays to create the new mask
            data_mask = ma.getmaskarray(tile.times)[:, np.newaxis, np.newaxis] \
                        | ma.getmaskarray(tile.latitudes)[np.newaxis, :, np.newaxis] \
                        | ma.getmaskarray(tile.longitudes)[np.newaxis, np.newaxis, :]

            tile.data = ma.masked_where(data_mask, tile.data)

        return tiles

    def fetch_data_for_tiles(self, *tiles):

        nexus_tile_ids = set([tile.tile_id for tile in tiles])
        matched_tile_data = self._cass.fetch_nexus_tiles(*nexus_tile_ids)
        tile_data_by_id = {str(a_tile_data.tile_id): a_tile_data for a_tile_data in matched_tile_data}

        missing_data = nexus_tile_ids.difference(tile_data_by_id.keys())
        if len(missing_data) > 0:
            raise StandardError("Missing data for tile_id(s) %s." % missing_data)

        for a_tile in tiles:
            lats, lons, times, data, meta = tile_data_by_id[a_tile.tile_id].get_lat_lon_time_data_meta()

            a_tile.latitudes = lats
            a_tile.longitudes = lons
            a_tile.times = times
            a_tile.data = data
            a_tile.meta_data = meta

            del (tile_data_by_id[a_tile.tile_id])

        return tiles

    def _solr_docs_to_tiles(self, *solr_docs):

        tiles = []
        for solr_doc in solr_docs:
            tile = Tile()
            tile.tile_id = solr_doc['id']
            tile.bbox = BBox(
                solr_doc['tile_min_lat'], solr_doc['tile_max_lat'],
                solr_doc['tile_min_lon'], solr_doc['tile_max_lon'])
            tile.dataset = solr_doc['dataset_s']
            # tile.dataset_id = solr_doc['dataset_id_s']
            tile.granule = solr_doc['granule_s']
            tile.min_time = solr_doc['tile_min_time_dt']
            tile.max_time = solr_doc['tile_max_time_dt']
            tile.section_spec = solr_doc['sectionSpec_s']
            tile.tile_stats = TileStats(
                solr_doc['tile_min_val_d'], solr_doc['tile_max_val_d'],
                solr_doc['tile_avg_val_d'], solr_doc['tile_count_i']
            )

            tiles.append(tile)

        return tiles

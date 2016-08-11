import uuid

import nexusproto.NexusContent_pb2 as nexusproto
import numpy as np
from cassandra.cqlengine import columns
from cassandra.cqlengine import connection
from cassandra.cqlengine.models import Model
from cassandra.policies import TokenAwarePolicy, DCAwareRoundRobinPolicy
from nexusproto.serialization import from_shaped_array


class NexusTileData(Model):
    __table_name__ = 'sea_surface_temp'
    tile_id = columns.UUID(primary_key=True)
    tile_blob = columns.Blob()

    __nexus_tile = None

    def _get_nexus_tile(self):
        if self.__nexus_tile is None:
            self.__nexus_tile = nexusproto.TileData.FromString(self.tile_blob)

        return self.__nexus_tile

    def get_raw_data_array(self):

        nexus_tile = self._get_nexus_tile()
        the_tile_type = nexus_tile.tile.WhichOneof("tile_type")

        the_tile_data = getattr(nexus_tile.tile, the_tile_type)

        return from_shaped_array(the_tile_data.variable_data)

    def get_lat_lon_time_data_meta(self):
        if self._get_nexus_tile().HasField('grid_tile'):
            grid_tile = self._get_nexus_tile().grid_tile

            grid_tile_data = np.ma.masked_invalid(from_shaped_array(grid_tile.variable_data))
            latitude_data = np.ma.masked_invalid(from_shaped_array(grid_tile.latitude))
            longitude_data = np.ma.masked_invalid(from_shaped_array(grid_tile.longitude))

            if len(grid_tile_data.shape) == 2:
                grid_tile_data = grid_tile_data[np.newaxis, :]

            # Extract the meta data
            meta_data = {}
            for meta_data_obj in grid_tile.meta_data:
                name = meta_data_obj.name
                meta_array = meta_data_obj.meta_data
                if len(meta_array.shape) == 2:
                    meta_array = meta_array[np.newaxis, :]
                meta_data[name] = meta_array

            return latitude_data, longitude_data, np.array([grid_tile.time]), grid_tile_data, meta_data
        elif self._get_nexus_tile().HasField('swath_tile'):
            swath_tile = self._get_nexus_tile().swath_tile

            latitude_data = np.ma.masked_invalid(from_shaped_array(swath_tile.latitude)).reshape(-1)
            longitude_data = np.ma.masked_invalid(from_shaped_array(swath_tile.longitude)).reshape(-1)
            time_data = np.ma.masked_invalid(from_shaped_array(swath_tile.time)).reshape(-1)

            swath_tile_data = np.ma.masked_invalid(from_shaped_array(swath_tile.variable_data))
            tile_data = np.ma.masked_all((len(time_data), len(latitude_data), len(longitude_data)))

            row, col = np.indices(swath_tile_data.shape)

            tile_data[np.diag_indices(len(time_data), 3)] = swath_tile_data[row.flat, col.flat]
            tile_data.mask[np.diag_indices(len(time_data), 3)] = swath_tile_data.mask[row.flat, col.flat]

            del swath_tile_data

            # Extract the meta data
            meta_data = {}
            for meta_data_obj in swath_tile.meta_data:
                name = meta_data_obj.name
                actual_meta_array = np.ma.masked_invalid(from_shaped_array(meta_data_obj.meta_data))
                reshaped_meta_array = np.ma.masked_all((len(time_data), len(latitude_data), len(longitude_data)))
                row, col = np.indices(actual_meta_array.shape)
                reshaped_meta_array[np.diag_indices(len(time_data), 3)] = actual_meta_array[row.flat, col.flat]
                reshaped_meta_array.mask[np.diag_indices(len(time_data), 3)] = actual_meta_array.mask[row.flat, col.flat]
                meta_data[name] = reshaped_meta_array

            return latitude_data, longitude_data, time_data, tile_data, meta_data
        else:
            raise NotImplementedError("Only supports grid_tile and swath_tile")


class CassandraProxy(object):
    def __init__(self, config):
        self.config = config
        self.__cass_url = config.get("cassandra", "host")
        self.__cass_keyspace = config.get("cassandra", "keyspace")
        self.__cass_local_DC = config.get("cassandra", "local_datacenter")
        self.__cass_protocol_version = int(config.get("cassandra", "protocol_version"))
        self.__open()

    def __open(self):
        dc_policy = DCAwareRoundRobinPolicy(self.__cass_local_DC)
        token_policy = TokenAwarePolicy(dc_policy)
        connection.setup([host for host in self.__cass_url.split(',')], self.__cass_keyspace,
                         protocol_version=self.__cass_protocol_version, load_balancing_policy=token_policy)

    def fetch_nexus_tiles(self, *tile_ids):
        tile_ids = [uuid.UUID(str(tile_id)) for tile_id in tile_ids if
                    (isinstance(tile_id, str) or isinstance(tile_id, unicode))]

        res = [NexusTileData.objects.filter(tile_id=tile_id)[0] for tile_id in tile_ids]

        return res
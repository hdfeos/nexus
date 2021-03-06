# distutils: include_dirs = /usr/local/lib/python2.7/site-packages/cassandra
#import pyximport

#pyximport.install()

import sys
import os
import math
import numpy as np
from time import time
import itertools
from webservice.SparkAlg import SparkAlg
from webservice.NexusHandler import NexusHandler, nexus_handler, DEFAULT_PARAMETERS_SPEC
from nexustiles.nexustiles import NexusTileService
from webservice.webmodel import NexusResults, NexusProcessingException, NoDataException
from pyspark import SparkContext,SparkConf

@nexus_handler
class TimeAvgMapSparkHandlerImpl(SparkAlg):

    name = "Time Average Map Spark"
    path = "/timeAvgMapSpark"
    description = "Computes a Latitude/Longitude Time Average plot given an arbitrary geographical area and time range"
    params = DEFAULT_PARAMETERS_SPEC
    singleton = True

    def __init__(self):
        SparkAlg.__init__(self)

    @staticmethod
    def _map(tile_in_spark):
        tile_bounds = tile_in_spark[0]
        (min_lat, max_lat, min_lon, max_lon,
         min_y, max_y, min_x, max_x) = tile_bounds
        startTime = tile_in_spark[1]
        endTime = tile_in_spark[2]
        ds = tile_in_spark[3]
        cwd = tile_in_spark[4]
        os.chdir(cwd)
        tile_service = NexusTileService()
        print 'Started tile', tile_bounds
        sys.stdout.flush()
        tile_inbounds_shape = (max_y-min_y+1, max_x-min_x+1)
        days_at_a_time = 90
        #days_at_a_time = 7
        #days_at_a_time = 1
        print 'days_at_a_time = ', days_at_a_time
        t_incr = 86400 * days_at_a_time
        sum_tile = np.array(np.zeros(tile_inbounds_shape, dtype=np.float64))
        cnt_tile = np.array(np.zeros(tile_inbounds_shape, dtype=np.uint32))
        t_start = startTime
        while t_start <= endTime:
            t_end = min(t_start+t_incr,endTime)
            t1 = time()
            print 'nexus call start at time %f' % t1
            sys.stdout.flush()
            nexus_tiles = \
                TimeAvgMapSparkHandlerImpl.query_by_parts(tile_service,
                                                          min_lat, max_lat, 
                                                          min_lon, max_lon, 
                                                          ds, 
                                                          t_start, 
                                                          t_end,
                                                          part_dim=2)
            t2 = time()
            print 'nexus call end at time %f' % t2
            print 'secs in nexus call: ', t2-t1
            sys.stdout.flush()
            TimeAvgMapSparkHandlerImpl._prune_tiles(nexus_tiles)
            print 't %d to %d - Got %d tiles' % (t_start, t_end, 
                                                 len(nexus_tiles))
            sys.stdout.flush()
            for tile in nexus_tiles:
                tile.data.data[:,:] = np.nan_to_num(tile.data.data)
                sum_tile += tile.data.data[0,min_y:max_y+1,min_x:max_x+1]
                cnt_tile += (~tile.data.mask[0,
                                             min_y:max_y+1,
                                             min_x:max_x+1]).astype(np.uint8)
            t_start = t_end + 1

        #print 'cnt_tile = ', cnt_tile
        #cnt_tile.mask = ~(cnt_tile.data.astype(bool))
        #sum_tile.mask = cnt_tile.mask
        #avg_tile = sum_tile / cnt_tile
        #stats_tile = [[{'avg': avg_tile.data[y,x], 'cnt': cnt_tile.data[y,x]} for x in range(tile_inbounds_shape[1])] for y in range(tile_inbounds_shape[0])]
        print 'Finished tile', tile_bounds
        #print 'Tile avg = ', avg_tile
        sys.stdout.flush()
        return ((min_lat,max_lat,min_lon,max_lon),(sum_tile,cnt_tile))

    def calc(self, computeOptions, **args):
        """

        :param computeOptions: StatsComputeOptions
        :param args: dict
        :return:
        """

        self._setQueryParams(computeOptions.get_dataset()[0],
                             (float(computeOptions.get_min_lat()),
                              float(computeOptions.get_max_lat()),
                              float(computeOptions.get_min_lon()),
                              float(computeOptions.get_max_lon())),
                             computeOptions.get_start_time(),
                             computeOptions.get_end_time())
      
        self._find_native_resolution()
        print 'Using Native resolution: lat_res=%f, lon_res=%f' % (self._latRes, self._lonRes)
        self._minLatCent = self._minLat + self._latRes / 2
        self._minLonCent = self._minLon + self._lonRes / 2
        nlats = int((self._maxLat-self._minLatCent)/self._latRes)+1
        nlons = int((self._maxLon-self._minLonCent)/self._lonRes)+1
        self._maxLatCent = self._minLatCent + (nlats-1) * self._latRes
        self._maxLonCent = self._minLonCent + (nlons-1) * self._lonRes
        print 'nlats=',nlats,'nlons=',nlons
        print 'center lat range = %f to %f' % (self._minLatCent, 
                                               self._maxLatCent)
        print 'center lon range = %f to %f' % (self._minLonCent, 
                                               self._maxLonCent)
        sys.stdout.flush()
        a = np.zeros((nlats, nlons),dtype=np.float64,order='C')
        n = np.zeros((nlats, nlons),dtype=np.float64,order='C')

        nexus_tiles = self._find_global_tile_set()
        # print 'tiles:'
        # for tile in nexus_tiles:
        #     print tile.granule
        #     print tile.section_spec
        #     print 'lat:', tile.latitudes
        #     print 'lon:', tile.longitudes

        #                                                          nexus_tiles)
        if len(nexus_tiles) == 0:
            raise NexusProcessingException.NoDataException(reason="No data found for selected timeframe")

        print 'Initially found %d tiles' % len(nexus_tiles)
        sys.stdout.flush()
        self._prune_tiles(nexus_tiles)
        print 'Pruned to %d tiles' % len(nexus_tiles)
        sys.stdout.flush()
        #for tile in nexus_tiles:
        #    print 'lats: ', tile.latitudes.compressed()
        #    print 'lons: ', tile.longitudes.compressed()
        # Create array of tuples to pass to Spark map function
        cwd = os.getcwd()
        nexus_tiles_spark = [[self._find_tile_bounds(t), 
                              self._startTime, self._endTime, 
                              self._ds, cwd] for t in nexus_tiles]
        #print 'nexus_tiles_spark = ', nexus_tiles_spark
        # Remove empty tiles (should have bounds set to None)
        bad_tile_inds = np.where([t[0] is None for t in nexus_tiles_spark])[0]
        for i in np.flipud(bad_tile_inds):
            del nexus_tiles_spark[i]

        # Expand Spark map tuple array by duplicating each entry N times,
        # where N is the number of ways we want the time dimension carved up.
        num_time_parts = 18
       #nexus_tiles_spark = list(itertools.chain.from_iterable(itertools.repeat(t, num_time_parts) for t in nexus_tiles_spark))
        nexus_tiles_spark = np.repeat(nexus_tiles_spark, num_time_parts, axis=0)
        print 'repeated len(nexus_tiles_spark) = ', len(nexus_tiles_spark)
        
        # Set the time boundaries for each of the Spark map tuples.
        # Every Nth element in the array gets the same time bounds.
        spark_part_times = np.linspace(self._startTime, self._endTime,
                                       num_time_parts+1, dtype=np.int64)
        
        spark_part_time_ranges = \
            np.repeat([[[spark_part_times[i], 
                         spark_part_times[i+1]] for i in range(num_time_parts)]],
                      len(nexus_tiles_spark) / num_time_parts, axis=0).reshape((len(nexus_tiles_spark), 2))
        print 'spark_part_time_ranges=', spark_part_time_ranges
        nexus_tiles_spark[:,1:3] = spark_part_time_ranges
        print 'nexus_tiles_spark final = '
        for i in range(len(nexus_tiles_spark)):
            print nexus_tiles_spark[i]

        # Configure Spark
        sp_conf = SparkConf()
        sp_conf.setAppName("Spark Time Avg Map")
        sp_conf.set("spark.executorEnv.HOME",
                    os.path.join(os.getenv('HOME'), 'spark_exec_home'))
        sp_conf.set("spark.executorEnv.PYTHONPATH", cwd)
        #sp_conf.set("spark.yarn.executor.memoryOverhead", "4000")
        sp_conf.set("spark.executor.memory", "4g")

        #num_parts = 1
        num_parts = 16
        #num_parts = 64
        #num_parts = 128
        #num_execs = 1
        num_execs = 16
        #num_execs = 64
        cores_per_exec = 1
        sp_conf.setMaster("yarn-client")
        #sp_conf.setMaster("local[16]")
        #sp_conf.setMaster("local[1]")
        sp_conf.set("spark.executor.instances", num_execs)
        sp_conf.set("spark.executor.cores", cores_per_exec)

        #print sp_conf.getAll()
        sc = SparkContext(conf=sp_conf)
        
        # Launch Spark computations
        rdd = sc.parallelize(nexus_tiles_spark,num_parts)
        sum_count_part = rdd.map(self._map)
        sum_count = \
            sum_count_part.combineByKey(lambda val: val,
                                        lambda x,val: (x[0]+val[0], 
                                                       x[1]+val[1]),
                                        lambda x,y: (x[0]+y[0], x[1]+y[1]))
        avg_tiles = \
            sum_count.map(lambda (bounds, (sum_tile, cnt_tile)):
                              (bounds, [[{'avg': (sum_tile[y,x]/cnt_tile[y,x]) 
                                          if (cnt_tile[y,x] > 0) else 0., 
                                          'cnt': cnt_tile[y,x]} 
                                         for x in 
                                         range(sum_tile.shape[1])] 
                                        for y in 
                                        range(sum_tile.shape[0])])).collect()

        #avg_tiles = map(self._map, nexus_tiles)

        # Combine subset results to produce global map.
        #
        # The tiles below are NOT Nexus objects.  They are tuples
        # with the time avg map data and lat-lon bounding box.
        for tile in avg_tiles:
            if tile is not None:
                ((tile_min_lat, tile_max_lat, tile_min_lon, tile_max_lon),
                 tile_stats) = tile
                tile_data = np.ma.array([[tile_stats[y][x]['avg'] for x in range(len(tile_stats[0]))] for y in range(len(tile_stats))])
                tile_cnt = np.array([[tile_stats[y][x]['cnt'] for x in range(len(tile_stats[0]))] for y in range(len(tile_stats))])
                tile_data.mask = ~(tile_cnt.astype(bool))
                y0 = self._lat2ind(tile_min_lat)
                y1 = y0 + tile_data.shape[0] - 1
                x0 = self._lon2ind(tile_min_lon)
                x1 = x0 + tile_data.shape[1] - 1
                if np.any(np.logical_not(tile_data.mask)):
                    print 'writing tile lat %f-%f, lon %f-%f, map y %d-%d, map x %d-%d' % \
                        (tile_min_lat, tile_max_lat, 
                         tile_min_lon, tile_max_lon, y0, y1, x0, x1)
                    sys.stdout.flush()
                    a[y0:y1+1,x0:x1+1] = tile_data
                    n[y0:y1+1,x0:x1+1] = tile_cnt
                else:
                    print 'All pixels masked in tile lat %f-%f, lon %f-%f, map y %d-%d, map x %d-%d' % \
                        (tile_min_lat, tile_max_lat, 
                         tile_min_lon, tile_max_lon, y0, y1, x0, x1)
                    sys.stdout.flush()

        # Store global map in a NetCDF file.
        self._create_nc_file(a, 'tam.nc', 'val')

        # Create dict for JSON response
        results = [[{'avg': a[x,y], 'cnt': n[x,y]}
                    for x in range(a.shape[0])] for y in range(a.shape[1])]
        return TimeAvgMapSparkResults(results=results, meta={}, computeOptions=computeOptions)


class TimeAvgMapSparkResults(NexusResults):

    def __init__(self, results=None, meta=None, computeOptions=None):
        NexusResults.__init__(self, results=results, meta=meta, stats=None, computeOptions=computeOptions)

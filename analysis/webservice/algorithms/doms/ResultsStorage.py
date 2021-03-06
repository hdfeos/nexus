"""
Copyright (c) 2016 Jet Propulsion Laboratory,
California Institute of Technology.  All rights reserved
"""

import logging
import ConfigParser
import uuid
import nexusproto.NexusContent_pb2 as nexusproto
from cassandra.cqlengine import columns
from cassandra.cqlengine import connection
from cassandra.cqlengine.models import Model
from cassandra.policies import TokenAwarePolicy, DCAwareRoundRobinPolicy
from nexusproto.serialization import from_shaped_array
from cassandra.cluster import Cluster
import string
from datetime import datetime
from cassandra.query import BatchStatement

class AbstractResultsContainer:

    def __init__(self):
        self._log = logging.getLogger(__name__)
        self._log.info("Creating DOMS Results Storage Instance")

        domsconfig = ConfigParser.ConfigParser()
        domsconfig.read("webservice/algorithms/doms/domsconfig.ini")

        cassHost = domsconfig.get("cassandra", "host")
        cassKeyspace = domsconfig.get("cassandra", "keyspace")
        cassDatacenter = domsconfig.get("cassandra", "local_datacenter")
        cassVersion = domsconfig.get("cassandra", "protocol_version")

        dc_policy = DCAwareRoundRobinPolicy(cassDatacenter)
        token_policy = TokenAwarePolicy(dc_policy)

        cluster = Cluster(
                [host for host in cassHost.split(',')],
                load_balancing_policy=token_policy)

        self._session = cluster.connect(cassKeyspace)

    def generateExecutionId(self):
        return str(uuid.uuid4())

    def _parseDatetime(self, dtString):
        dt = datetime.strptime(dtString, "%Y-%m-%dT%H:%M:%SZ")
        epoch = datetime.utcfromtimestamp(0)
        time = (dt - epoch).total_seconds() * 1000.0
        return int(time)

class ResultsStorage(AbstractResultsContainer):

    def __init__(self):
        AbstractResultsContainer.__init__(self)

    def insertResults(self, results, params, stats, startTime, completeTime, userEmail):
        id = self.generateExecutionId()

        self.__insertExecution(id, startTime, completeTime, userEmail)
        self.__insertParams(id, params)
        self.__insertStats(id, stats)
        self.__insertResults(id, results)
        return id


    def __insertExecution(self, id, startTime, completeTime, userEmail):
        cql = "INSERT INTO doms_executions (id, time_started, time_completed, user_email) VALUES (%s, %s, %s, %s)"
        self._session.execute(cql, (id, startTime, completeTime, userEmail))

    def __insertParams(self, id, params):
        cql = """INSERT INTO doms_params
                    (execution_id, primary_dataset, matchup_datasets, depth_tolerance, time_tolerance, radius_tolerance, start_time, end_time, platforms, bounding_box)
                 VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        self._session.execute(cql, (id,
                                     params["primary"],
                                     string.join(params["matchup"], ","),
                                     params["depthTolerance"],
                                     int(params["timeTolerance"]),
                                     params["radiusTolerance"],
                                     self._parseDatetime(params["startTime"]),
                                     self._parseDatetime(params["endTime"]),
                                     params["platforms"],
                                     params["bbox"]
                                     ))


    def __insertStats(self, id, stats):
        cql = """
           INSERT INTO doms_execution_stats
                (execution_id, num_gridded_matched, num_gridded_checked, num_insitu_matched, num_insitu_checked, time_to_complete)
           VALUES
                (%s, %s, %s, %s, %s, %s)
        """
        self._session.execute(cql, (
            id,
            stats["numGriddedMatched"],
            stats["numGriddedChecked"],
            stats["numInSituMatched"],
            stats["numInSituRecords"],
            stats["timeToComplete"]
        ))


    def __insertResults(self, id, results):

        cql = """
           INSERT INTO doms_data
                (id, execution_id, value_id, primary_value_id, x, y, source_dataset, measurement_time, platform, device, measurement_values, is_primary)
           VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        insertStatement = self._session.prepare(cql)
        batch = BatchStatement()


        for result in results:
            self.__insertResult(id, None, result, batch, insertStatement)



        self._session.execute(batch)

    def __insertResult(self, id, primaryId, result, batch, insertStatement):

        dataMap = self.__buildDataMap(result)
        batch.add(insertStatement,(
                    self.generateExecutionId(),
                    id,
                    result["id"],
                    primaryId,
                    result["x"],
                    result["y"],
                    result["source"],
                    int(result["time"]),
                    result["platform"] if "platform" in result else None,
                    result["device"] if "device" in result else None,
                    dataMap,
                    1 if primaryId is None else 0
                )
        )

        n = 0
        if "matches" in result:
            for match in result["matches"]:
                self.__insertResult(id, result["id"], match, batch, insertStatement)
                n += 1
                if n >= 20:
                    if primaryId is None:
                        self.__commitBatch(batch)
                    n = 0

        if primaryId is None:
            self.__commitBatch(batch)

    def __commitBatch(self, batch):
        self._session.execute(batch)
        batch.clear()

    def __buildDataMap(self, result):
        dataMap = {}
        for name in result:
            value = result[name]
            if name not in ["id", "x", "y", "source", "time", "platform", "device", "point", "matches"] and type(value) in [float, int]:
                dataMap[name] = value
        return dataMap




class ResultsRetrieval(AbstractResultsContainer):

    def __init__(self):
        AbstractResultsContainer.__init__(self)

    def retrieveResults(self, id):
        params = self.__retrieveParams(id)
        stats = self.__retrieveStats(id)
        data = self.__retrieveData(id)
        return params, stats, data


    def __retrieveData(self, id):
        dataMap = self.__retrievePrimaryData(id)
        self.__enrichPrimaryDataWithMatches(id, dataMap)
        data = [dataMap[name] for name in dataMap]
        return data

    def __enrichPrimaryDataWithMatches(self, id, dataMap):
        cql = "SELECT * FROM doms_data where execution_id = %s and is_primary = 0 ALLOW FILTERING"
        rows = self._session.execute(cql, (id,))

        for row in rows:
            entry = self.__rowToDataEntry(row)
            if row.primary_value_id in dataMap:
                if not "matches" in dataMap[row.primary_value_id]:
                    dataMap[row.primary_value_id]["matches"] = []
                dataMap[row.primary_value_id]["matches"].append(entry)
            else:
                print row

    def __retrievePrimaryData(self, id):
        cql = "SELECT * FROM doms_data where execution_id = %s and is_primary = 1 ALLOW FILTERING"
        rows = self._session.execute(cql, (id,))

        dataMap = {}
        for row in rows:
            entry = self.__rowToDataEntry(row)
            dataMap[row.value_id] = entry
        return dataMap

    def __rowToDataEntry(self, row):
        entry = {
            "id": row.value_id,
            "x": row.x,
            "y": row.y,
            "source": row.source_dataset,
            "device": row.device,
            "platform": row.platform
        }
        for key in row.measurement_values:
            value = row.measurement_values[key]
            entry[key] = value
        return entry

    def __retrieveStats(self, id):
        cql = "SELECT * FROM doms_execution_stats where execution_id = %s limit 1"
        rows = self._session.execute(cql, (id,))
        for row in rows:
            stats = {
                "numGriddedMatched": row.num_gridded_matched,
                "numGriddedChecked": row.num_gridded_checked,
                "numInSituMatched": row.num_insitu_matched,
                "numInSituChecked": row.num_insitu_checked,
                "timeToComplete": row.time_to_complete
            }
            return stats

        raise Exception("Execution not found with id '%s'"%id)






    def __retrieveParams(self, id):
        cql = "SELECT * FROM doms_params where execution_id = %s limit 1"
        rows = self._session.execute(cql, (id,))
        for row in rows:
            params = {
                "primary": row.primary_dataset,
                "matchup": row.matchup_datasets.split(","),
                "depthTolerance": row.depth_tolerance,
                "timeTolerance": row.time_tolerance,
                "radiusTolerance": row.radius_tolerance,
                "startTime": row.start_time,
                "endTime": row.end_time,
                "platforms": row.platforms,
                "bbox": row.bounding_box
            }
            return params

        raise Exception("Execution not found with id '%s'"%id)


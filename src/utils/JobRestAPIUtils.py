import json
import os
import time
import argparse
import uuid
import subprocess
import sys
import collections

from jobs_tensorboard import GenTensorboardMeta

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),"../storage"))

import yaml
from jinja2 import Environment, FileSystemLoader, Template
from config import config
from DataHandler import DataHandler,DataManager
import base64
import re

from config import global_vars
from authorization import ResourceType, Permission, AuthorizationManager, IdentityManager
import authorization
from cache import CacheManager
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),"../ClusterManager"))
from ResourceInfo import ResourceInfo
import quota

import copy
import logging
from cachetools import cached, TTLCache
from threading import Lock


DEFAULT_JOB_PRIORITY = 100
USER_JOB_PRIORITY_RANGE = (100, 200)
ADMIN_JOB_PRIORITY_RANGE = (1, 1000)


logger = logging.getLogger(__name__)


def adjust_job_priority(priority, permission):
    priority_range = (DEFAULT_JOB_PRIORITY, DEFAULT_JOB_PRIORITY)
    if permission == Permission.User:
        priority_range = USER_JOB_PRIORITY_RANGE
    elif permission == Permission.Admin:
        priority_range = ADMIN_JOB_PRIORITY_RANGE

    if priority > priority_range[1]:
        priority = priority_range[1]
    elif priority < priority_range[0]:
        priority = priority_range[0]

    return priority


def LoadJobParams(jobParamsJsonStr):
    return json.loads(jobParamsJsonStr)


def ToBool(value):
    if isinstance(value, basestring):
        value = str(value)
        if str.isdigit(value):
            ret = int(value)
            if ret == 0:
                return False
            else:
                return True
        else:
            if value.upper() == 'TRUE':
                return True
            elif value.upper() == 'FALSE':
                return False
            else:
                raise ValueError
    elif isinstance(value, int):
        if value == 0:
            return False
        else:
            return True
    else:
        return value


def SubmitJob(jobParamsJsonStr):
    ret = {}

    jobParams = LoadJobParams(jobParamsJsonStr)

    if "jobName" not in jobParams or len(jobParams["jobName"].strip()) == 0:
        ret["error"] = "ERROR: Job name cannot be empty"
        return ret
    if "vcName" not in jobParams or len(jobParams["vcName"].strip()) == 0:
        ret["error"] = "ERROR: VC name cannot be empty"
        return ret
    if "userId" not in jobParams or len(jobParams["userId"].strip()) == 0:
        jobParams["userId"] = GetUser(jobParams["userName"])["uid"]

    if "preemptionAllowed" not in jobParams:
        jobParams["preemptionAllowed"] = False
    else:
        jobParams["preemptionAllowed"] = ToBool(jobParams["preemptionAllowed"])

    if "jobId" not in jobParams or jobParams["jobId"] == "":
        #jobParams["jobId"] = jobParams["jobName"] + "-" + str(uuid.uuid4())
        #jobParams["jobId"] = jobParams["jobName"] + "-" + str(time.time())
        jobParams["jobId"] = str(uuid.uuid4())
    #jobParams["jobId"] = jobParams["jobId"].replace("_","-").replace(".","-")

    if "resourcegpu" not in jobParams:
        jobParams["resourcegpu"] = 0

    if isinstance(jobParams["resourcegpu"], basestring):
        if len(jobParams["resourcegpu"].strip()) == 0:
            jobParams["resourcegpu"] = 0
        else:
            jobParams["resourcegpu"] = int(jobParams["resourcegpu"])

    if "familyToken" not in jobParams or jobParams["familyToken"].isspace():
        jobParams["familyToken"] = str(uuid.uuid4())
    if "isParent" not in jobParams:
        jobParams["isParent"] = 1

    userName = jobParams["userName"]
    if "@" in userName:
        userName = userName.split("@")[0].strip()

    if "/" in userName:
        userName = userName.split("/")[1].strip()

    if not AuthorizationManager.HasAccess(jobParams["userName"], ResourceType.VC, jobParams["vcName"].strip(), Permission.User):
        ret["error"] = "Access Denied!"
        return ret

    if "cmd" not in jobParams:
        jobParams["cmd"] = ""

    if "jobPath" in jobParams and len(jobParams["jobPath"].strip()) > 0:
        jobPath = jobParams["jobPath"]
        if ".." in jobParams["jobPath"]:
            ret["error"] = "ERROR: '..' cannot be used in job directory"
            return ret

        if "\\." in jobParams["jobPath"]:
            ret["error"] = "ERROR: invalided job directory"
            return ret

        if jobParams["jobPath"].startswith("/") or jobParams["jobPath"].startswith("\\"):
            ret["error"] = "ERROR: job directory should not start with '/' or '\\' "
            return ret

        if not jobParams["jobPath"].startswith(userName):
            jobParams["jobPath"] = os.path.join(userName,jobParams["jobPath"])

    else:
        jobPath = userName+"/"+ "jobs/"+time.strftime("%y%m%d")+"/"+jobParams["jobId"]
        jobParams["jobPath"] = jobPath

    if "workPath" not in jobParams or len(jobParams["workPath"].strip()) == 0:
       jobParams["workPath"] = "."

    if ".." in jobParams["workPath"]:
        ret["error"] = "ERROR: '..' cannot be used in work directory"
        return ret

    if "\\." in jobParams["workPath"]:
        ret["error"] = "ERROR: invalided work directory"
        return ret

    if jobParams["workPath"].startswith("/") or jobParams["workPath"].startswith("\\"):
        ret["error"] = "ERROR: work directory should not start with '/' or '\\' "
        return ret

    if not jobParams["workPath"].startswith(userName):
        jobParams["workPath"] = os.path.join(userName,jobParams["workPath"])

    if "dataPath" not in jobParams or len(jobParams["dataPath"].strip()) == 0:
        jobParams["dataPath"] = "."

    if ".." in jobParams["dataPath"]:
        ret["error"] = "ERROR: '..' cannot be used in data directory"
        return ret

    if "\\." in jobParams["dataPath"]:
        ret["error"] = "ERROR: invalided data directory"
        return ret

    if jobParams["dataPath"][0] == "/" or jobParams["dataPath"][0] == "\\":
        ret["error"] = "ERROR: data directory should not start with '/' or '\\' "
        return ret

    jobParams["dataPath"] = jobParams["dataPath"].replace("\\","/")
    jobParams["workPath"] = jobParams["workPath"].replace("\\","/")
    jobParams["jobPath"] = jobParams["jobPath"].replace("\\","/")
    jobParams["dataPath"] = os.path.realpath(os.path.join("/",jobParams["dataPath"]))[1:]
    jobParams["workPath"] = os.path.realpath(os.path.join("/",jobParams["workPath"]))[1:]
    jobParams["jobPath"] = os.path.realpath(os.path.join("/",jobParams["jobPath"]))[1:]

    dataHandler = DataHandler()
    if "logDir" in jobParams and len(jobParams["logDir"].strip()) > 0:
        tensorboardParams = jobParams.copy()

        # overwrite for distributed job
        if tensorboardParams["jobtrainingtype"] == "PSDistJob":
            tensorboardParams["jobtrainingtype"] = "RegularJob"
            match = re.match('(.*)(/.*)', tensorboardParams["logDir"])
            if not match is None:
                newDir = match.group(1) + "/worker0" + match.group(2)
                prefix = match.group(1)
                match2 = re.match('.*/worker0', prefix)
                if match2 is None:
                    tensorboardParams["logDir"] = newDir
            #match = re.match('(.*--logdir\s+.*)(/.*--.*)', tensorboardParams["cmd"])
            #if not match is None:
            #    tensorboardParams["cmd"] = match.group(1) + "/worker0" + match.group(2)

        tensorboardParams["jobId"] = str(uuid.uuid4())
        tensorboardParams["jobName"] = "tensorboard-"+jobParams["jobName"]
        tensorboardParams["jobPath"] = jobPath
        tensorboardParams["jobType"] = "visualization"
        tensorboardParams["cmd"] = "tensorboard --logdir " + tensorboardParams["logDir"] + " --host 0.0.0.0"
        tensorboardParams["image"] = jobParams["image"]
        tensorboardParams["resourcegpu"] = 0

        tensorboardParams["interactivePort"] = "6006"


        if "error" not in ret:
            if not dataHandler.AddJob(tensorboardParams):
                ret["error"] = "Cannot schedule tensorboard job."


    if "error" not in ret:
        if dataHandler.AddJob(jobParams):
            ret["jobId"] = jobParams["jobId"]
            if "jobPriority" in jobParams:
                priority = DEFAULT_JOB_PRIORITY
                try:
                    priority = int(jobParams["jobPriority"])
                except Exception as e:
                    pass

                permission = Permission.User
                if AuthorizationManager.HasAccess(jobParams["userName"], ResourceType.VC, jobParams["vcName"].strip(), Permission.Admin):
                    permission = Permission.Admin

                priority = adjust_job_priority(priority, permission)

                job_priorities = {jobParams["jobId"]: priority}
                update_job_priorites(jobParams["userName"], job_priorities)
        else:
            ret["error"] = "Cannot schedule job. Cannot add job into database."





    dataHandler.Close()
    InvalidateJobListCache(jobParams["vcName"])
    return ret



def GetJobList(userName, vcName, jobOwner, num=None):
    try:
        dataHandler = DataHandler()
        jobs = []
        hasAccessOnAllJobs = False

        if AuthorizationManager.HasAccess(userName, ResourceType.VC, vcName, Permission.Collaborator):
            hasAccessOnAllJobs = True

        if jobOwner != "all" or not hasAccessOnAllJobs:
            jobs = jobs + GetUserPendingJobs(userName, vcName)
            jobs = jobs + dataHandler.GetJobList(userName,vcName,num, "running,queued,scheduling,unapproved,pausing,paused", ("<>","and"))
        else:
            jobs = GetUserPendingJobs(jobOwner, vcName)

        for job in jobs:
            job.pop('jobMeta', None)
        dataHandler.Close()
        return jobs
    except Exception as e:
        logger.error('Exception: %s', str(e))
        logger.warn("Fail to get job list for user %s, return empty list", userName)
        return []


def GetUserPendingJobs(userName, vcName):
    jobs = []
    allJobs = DataManager.GetAllPendingJobs(vcName)
    for job in allJobs:
        if userName == "all" or userName == job["userName"]:
            jobs.append(job)
    return jobs


def GetCommands(userName, jobId):
    commands = []
    dataHandler = DataHandler()
    jobs = dataHandler.GetJob(jobId=jobId)
    if jobs[0]["userName"] == userName or AuthorizationManager.HasAccess(userName, ResourceType.VC, jobs[0]["vcName"], Permission.Collaborator):
        commands = dataHandler.GetCommands(jobId=jobId)
    dataHandler.Close()
    return commands


def KillJob(userName, jobId):
    ret = False
    dataHandler = DataHandler()
    jobs = dataHandler.GetJob(jobId=jobId)
    if len(jobs) == 1 and jobs[0]["jobStatus"] in ["unapproved", "queued", "scheduling", "running", "paused", "pausing"]:
        job = jobs[0]
        if job["userName"] == userName or AuthorizationManager.HasAccess(userName, ResourceType.VC, job["vcName"], Permission.Admin):
            if job["isParent"] == 1:
                ret = True
                for currJob in dataHandler.GetJob(familyToken=job["familyToken"]):
                    ret = ret and dataHandler.UpdateJobTextField(currJob["jobId"],"jobStatus","killing")
            else:
                ret = dataHandler.UpdateJobTextField(jobId,"jobStatus","killing")
    dataHandler.Close()
    InvalidateJobListCache(jobs[0]["vcName"])
    return ret


def InvalidateJobListCache(vcName):
    CacheManager.Invalidate("GetAllPendingJobs", vcName)
    DataManager.GetAllPendingJobs(vcName)


def AddCommand(userName, jobId,command):
    dataHandler = DataHandler()
    ret = False
    jobs =  dataHandler.GetJob(jobId=jobId)
    if len(jobs) == 1:
        if jobs[0]["userName"] == userName or AuthorizationManager.HasAccess(userName, ResourceType.VC, jobs[0]["vcName"], Permission.Collaborator):
            ret = dataHandler.AddCommand(jobId,command)
    dataHandler.Close()
    return ret


def ApproveJob(userName, jobId):
    dataHandler = DataHandler()
    ret = False
    jobs =  dataHandler.GetJob(jobId=jobId)
    if len(jobs) == 1 and jobs[0]["jobStatus"] == "unapproved":
        if AuthorizationManager.HasAccess(userName, ResourceType.VC, jobs[0]["vcName"], Permission.Admin):
            ret = dataHandler.UpdateJobTextField(jobId,"jobStatus","queued")
    dataHandler.Close()
    InvalidateJobListCache(jobs[0]["vcName"])
    return ret


def ResumeJob(userName, jobId):
    dataHandler = DataHandler()
    ret = False
    jobs = dataHandler.GetJob(jobId=jobId)
    if len(jobs) == 1 and jobs[0]["jobStatus"] == "paused":
        if jobs[0]["userName"] == userName or AuthorizationManager.HasAccess(userName, ResourceType.VC, jobs[0]["vcName"], Permission.Collaborator):
            ret = dataHandler.UpdateJobTextField(jobId, "jobStatus", "unapproved")
    dataHandler.Close()
    return ret


def PauseJob(userName, jobId):
    dataHandler = DataHandler()
    ret = False
    jobs =  dataHandler.GetJob(jobId=jobId)
    if len(jobs) == 1 and jobs[0]["jobStatus"] in ["unapproved", "queued", "scheduling", "running"]:
        if jobs[0]["userName"] == userName or AuthorizationManager.HasAccess(userName, ResourceType.VC, jobs[0]["vcName"], Permission.Admin):
            ret = dataHandler.UpdateJobTextField(jobId,"jobStatus","pausing")
    dataHandler.Close()
    return ret


def isBase64(s):
    try:
        if base64.b64encode(base64.b64decode(s)) == s:
            return True
    except Exception as e:
        pass
    return False


def GetJobDetail(userName, jobId):
    job = None
    dataHandler = DataHandler()
    jobs =  dataHandler.GetJob(jobId=jobId)
    if len(jobs) == 1:
        if jobs[0]["userName"] == userName or AuthorizationManager.HasAccess(userName, ResourceType.VC, jobs[0]["vcName"], Permission.Collaborator):
            job = jobs[0]
            job["log"] = ""
            #jobParams = json.loads(base64.b64decode(job["jobMeta"]))
            #jobPath,workPath,dataPath = GetStoragePath(jobParams["jobPath"],jobParams["workPath"],jobParams["dataPath"])
            #localJobPath = os.path.join(config["storage-mount-path"],jobPath)
            #logPath = os.path.join(localJobPath,"joblog.txt")
            #print logPath
            #if os.path.isfile(logPath):
            #    with open(logPath, 'r') as f:
            #        log = f.read()
            #        job["log"] = log
            #    f.close()
            if "jobDescription" in job:
                job.pop("jobDescription",None)
            try:
                log = dataHandler.GetJobTextField(jobId,"jobLog")
                try:
                    if isBase64(log):
                        log = base64.b64decode(log)
                except Exception:
                    pass
                if log is not None:
                    job["log"] = log
            except:
                job["log"] = "fail-to-get-logs"
    dataHandler.Close()
    return job

def GetJobStatus(jobId):
    result = None
    dataHandler = DataHandler()
    jobs = dataHandler.GetJob(jobId=jobId)
    if len(jobs) == 1:
        key_list = ["jobStatus", "jobTime", "errorMsg"]
        result = {key: jobs[0][key] for key in key_list}
    dataHandler.Close()
    return result

def GetClusterStatus():
    cluster_status,last_update_time =  DataManager.GetClusterStatus()
    return cluster_status,last_update_time


def AddUser(username,uid,gid,groups):
    ret = None
    needToUpdateDB = False

    if uid == authorization.INVALID_ID:
        info = IdentityManager.GetIdentityInfoFromDB(username)
        if info["uid"] == authorization.INVALID_ID:
            needToUpdateDB = True
            info = IdentityManager.GetIdentityInfoFromAD(username)
        uid = info["uid"]
        gid = info["gid"]
        groups = info["groups"]
    else:
        needToUpdateDB = True

    if needToUpdateDB:
        dataHandler = DataHandler()
        ret =  dataHandler.UpdateIdentityInfo(username,uid,gid,groups)
        ret = ret & dataHandler.UpdateAclIdentityId(username,uid)
        dataHandler.Close()
    return ret


def GetUser(username):
    return IdentityManager.GetIdentityInfoFromDB(username)


def UpdateAce(userName, identityName, resourceType, resourceName, permissions):
    ret = None
    resourceAclPath = AuthorizationManager.GetResourceAclPath(resourceName, resourceType)
    if AuthorizationManager.HasAccess(userName, resourceType, resourceName, Permission.Admin):
        ret =  AuthorizationManager.UpdateAce(identityName, resourceAclPath, permissions, False)
    else:
        ret = "Access Denied!"
    return ret


def DeleteAce(userName, identityName, resourceType, resourceName):
    ret = None
    resourceAclPath = AuthorizationManager.GetResourceAclPath(resourceName, resourceType)
    if AuthorizationManager.HasAccess(userName, resourceType, resourceName, Permission.Admin):
        ret =  AuthorizationManager.DeleteAce(identityName, resourceAclPath)
    else:
        ret = "Access Denied!"
    return ret


def AddStorage(userName, vcName, url, storageType, metadata, defaultMountPath):
    ret = None
    dataHandler = DataHandler()
    if AuthorizationManager.IsClusterAdmin(userName):
        ret =  dataHandler.AddStorage(vcName, url, storageType, metadata, defaultMountPath)
    else:
        ret = "Access Denied!"
    dataHandler.Close()
    return ret


def ListStorages(userName, vcName):
    ret = []
    dataHandler = DataHandler()
    if AuthorizationManager.HasAccess(userName, ResourceType.VC, vcName, Permission.User):
        ret = dataHandler.ListStorages(vcName)
    dataHandler.Close()
    return ret


def DeleteStorage(userName, vcName, url):
    ret = None
    dataHandler = DataHandler()
    if AuthorizationManager.HasAccess(userName, ResourceType.VC, vcName, Permission.Admin):
        ret =  dataHandler.DeleteStorage(vcName, url)
    else:
        ret = "Access Denied!"
    dataHandler.Close()
    return ret


def UpdateStorage(userName, vcName, url, storageType, metadata, defaultMountPath):
    ret = None
    dataHandler = DataHandler()
    if AuthorizationManager.HasAccess(userName, ResourceType.VC, vcName, Permission.Admin):
        ret =  dataHandler.UpdateStorage(vcName, url, storageType, metadata, defaultMountPath)
    else:
        ret = "Access Denied!"
    dataHandler.Close()
    return ret


def AddVC(userName, vcName, quota, metadata):
    ret = None
    dataHandler = DataHandler()
    if AuthorizationManager.IsClusterAdmin(userName):
        ret =  dataHandler.AddVC(vcName, quota, metadata)
    else:
        ret = "Access Denied!"
    dataHandler.Close()
    return ret


@cached(cache=TTLCache(maxsize=10240, ttl=1800), lock=Lock())
def ListVCs(userName):
    ret = []
    vcList =  DataManager.ListVCs()
    for vc in vcList:
        if AuthorizationManager.HasAccess(userName, ResourceType.VC, vc["vcName"], Permission.User):
            vc['admin'] = AuthorizationManager.HasAccess(userName, ResourceType.VC, vc["vcName"], Permission.Admin)
            ret.append(vc)
    # web portal (client) can filter out Default VC
    return ret


def GetVC(userName, vcName):
    ret = None

    data_handler = DataHandler()

    cluster_status, _ = data_handler.GetClusterStatus()
    cluster_total = cluster_status["gpu_capacity"]
    cluster_available = cluster_status["gpu_avaliable"]
    cluster_reserved = cluster_status["gpu_reserved"]

    user_status = collections.defaultdict(lambda : ResourceInfo())
    user_status_preemptable = collections.defaultdict(lambda : ResourceInfo())

    vc_list =  data_handler.ListVCs()
    vc_info = {}
    vc_usage = collections.defaultdict(lambda :
            collections.defaultdict(lambda : 0))
    vc_preemptable_usage = collections.defaultdict(lambda :
            collections.defaultdict(lambda : 0))

    for vc in vc_list:
        vc_info[vc["vcName"]] = json.loads(vc["quota"])

    active_job_list = data_handler.GetActiveJobList()
    for job in active_job_list:
        jobParam = json.loads(base64.b64decode(job["jobParams"]))
        if "gpuType" in jobParam:
            if not jobParam["preemptionAllowed"]:
                vc_usage[job["vcName"]][jobParam["gpuType"]] += GetJobTotalGpu(jobParam)
            else:
                vc_preemptable_usage[job["vcName"]][jobParam["gpuType"]] += GetJobTotalGpu(jobParam)

    result = quota.calculate_vc_gpu_counts(cluster_total, cluster_available,
            cluster_reserved, vc_info, vc_usage)

    vc_total, vc_used, vc_available, vc_unschedulable = result

    for vc in vc_list:
        if vc["vcName"] == vcName and AuthorizationManager.HasAccess(userName, ResourceType.VC, vcName, Permission.User):

            num_active_jobs = 0
            for job in active_job_list:
                if job["vcName"] == vcName and job["jobStatus"] == "running":
                    num_active_jobs += 1
                    username = job["userName"]
                    jobParam = json.loads(base64.b64decode(job["jobParams"]))
                    if "gpuType" in jobParam:
                        if not jobParam["preemptionAllowed"]:
                            if username not in user_status:
                                user_status[username] = ResourceInfo()
                            user_status[username].Add(ResourceInfo({jobParam["gpuType"] : GetJobTotalGpu(jobParam)}))
                        else:
                            if username not in user_status_preemptable:
                                user_status_preemptable[username] = ResourceInfo()
                            user_status_preemptable[username].Add(ResourceInfo({jobParam["gpuType"] : GetJobTotalGpu(jobParam)}))

            vc["gpu_capacity"] = vc_total[vcName]
            vc["gpu_used"] = vc_used[vcName]
            vc["gpu_preemptable_used"] = vc_preemptable_usage[vcName]
            vc["gpu_unschedulable"] = vc_unschedulable[vcName]
            vc["gpu_avaliable"] = vc_available[vcName]
            vc["AvaliableJobNum"] = num_active_jobs
            vc["node_status"] = cluster_status["node_status"]
            vc["user_status"] = []
            for user_name, user_gpu in user_status.iteritems():
                # TODO: job_manager.getAlias should be put in a util file
                user_name = user_name.split("@")[0].strip()
                vc["user_status"].append({"userName":user_name, "userGPU":user_gpu.ToSerializable()})

            vc["user_status_preemptable"] = []
            for user_name, user_gpu in user_status_preemptable.iteritems():
                user_name = user_name.split("@")[0].strip()
                vc["user_status_preemptable"].append({"userName": user_name, "userGPU": user_gpu.ToSerializable()})

            ret = vc
            break
    return ret


def GetJobTotalGpu(jobParams):
    numWorkers = 1
    if "numpsworker" in jobParams:
        numWorkers = int(jobParams["numpsworker"])
    return int(jobParams["resourcegpu"]) * numWorkers


def DeleteVC(userName, vcName):
    ret = None
    dataHandler = DataHandler()
    if AuthorizationManager.IsClusterAdmin(userName):
        ret =  dataHandler.DeleteVC(vcName)
    else:
        ret = "Access Denied!"
    dataHandler.Close()
    return ret


def UpdateVC(userName, vcName, quota, metadata):
    ret = None
    dataHandler = DataHandler()
    if AuthorizationManager.IsClusterAdmin(userName):
        ret =  dataHandler.UpdateVC(vcName, quota, metadata)
    else:
        ret = "Access Denied!"
    dataHandler.Close()
    return ret


def get_job(job_id):
    data_handler = None
    try:
        data_handler = DataHandler()
        jobs = data_handler.GetJob(jobId=job_id)
        if len(jobs) == 1:
            return jobs[0]
    except Exception as e:
        logger.error("Exception in get_job: %s" % str(e))
    finally:
        if data_handler is not None:
            data_handler.Close()
    return None


def update_job(job_id, field, value):
    dataHandler = DataHandler()
    dataHandler.UpdateJobTextField(job_id, field, value)
    dataHandler.Close()


def get_job_priorities():
    dataHandler = DataHandler()
    job_priorites = dataHandler.get_job_priority()
    dataHandler.Close()
    return job_priorites


def update_job_priorites(username, job_priorities):
    data_handler = None
    try:
        data_handler = DataHandler()

        # Only job owner and VC admin can update job priority.
        # Fail job priority update if there is one unauthorized items.
        for job_id in job_priorities:
            priority = job_priorities[job_id]
            jobs = data_handler.GetJob(jobId=job_id)
            if len(jobs) == 0:
                logger.warn("Update priority %s for non-existent job %s" %
                            (priority, job_id))
                continue

            if len(jobs) > 1:
                logger.warn("Multiple job entries found that matches job %s. "
                            "Most likely a platform bug." % job_id)

            job = jobs[0]
            vc_admin = AuthorizationManager.HasAccess(username, ResourceType.VC, job["vcName"], Permission.Admin)
            if job["userName"] != username and (not vc_admin):
                return False

            # Adjust priority based on permission
            permission = Permission.Admin if vc_admin else Permission.User
            job_priorities[job_id] = adjust_job_priority(priority, permission)

        ret_code = data_handler.update_job_priority(job_priorities)
        return ret_code

    except Exception as e:
        logger.error("Exception when updating job priorities: %s" % e)

    finally:
        if data_handler is not None:
            data_handler.Close()


if __name__ == '__main__':
    TEST_SUB_REG_JOB = False
    TEST_JOB_STATUS = True
    TEST_DEL_JOB = False
    TEST_GET_TB = False
    TEST_GET_SVC = False
    TEST_GET_LOG = False

    if TEST_SUB_REG_JOB:
        parser = argparse.ArgumentParser(description='Launch a kubernetes job')
        parser.add_argument('-f', '--param-file', required=True, type=str,
                            help = 'Path of the Parameter File')
        parser.add_argument('-t', '--template-file', required=True, type=str,
                            help = 'Path of the Job Template File')
        args, unknown = parser.parse_known_args()
        with open(args.param_file,"r") as f:
            jobParamsJsonStr = f.read()
        f.close()

        SubmitRegularJob(jobParamsJsonStr,args.template_file)

    if TEST_JOB_STATUS:
        print GetJobStatus(sys.argv[1])

    if TEST_DEL_JOB:
        print DeleteJob("tf-dist-1483504085-13")

    if TEST_GET_TB:
        print GetTensorboard("tf-resnet18-1483509537-31")

    if TEST_GET_SVC:
        print GetServiceAddress("tf-i-1483566214-12")

    if TEST_GET_LOG:
        print GetLog("tf-i-1483566214-12")

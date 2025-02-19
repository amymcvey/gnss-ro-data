#!/usr/bin/env python3

#  Imports.

import json
import copy
import boto3
import s3fs
import aiobotocore
import os
import re
import sys
from datetime import datetime, timedelta
import argparse

from ..Reformatters import reformatters
from ..Versions import get_version, valid_versions
from ..Utilities import s3fsauth
from ..Database.dynamodbinterface import process_reformat_wrapper
from ..Missions import valid_missions, get_receiver_satellites
from ..Utilities.resources import AWSregion as default_region

#  Logger.

import logging

LOGGER = logging.getLogger(__name__)

#  Exception handling.

class Error( Exception ):
    pass

class definejobsError( Error ):
    def __init__( self, message, comment ):
        self.message = message
        self.comment = comment

class DASKjobsError( Error ):
    def __init__( self, message, comment ):
        self.message = message
        self.comment = comment


################################################################################
#  definejobs
################################################################################

def definejobs( daterange, missions, processing_centers, file_types, version,
        UCARprefix=None, ROMSAFprefix=None, JPLprefix=None, EUMETSATprefix=None,
        nonnominal=False, session=None, liveupdate=False ):
    """Generate a listing of jobs for translation for a given date range, lists of
    missions, processing centers and AWS file types. The date range is a two-element
    tuple or list of instance of datetime.datetime that defines an inclusive list of
    dates over which to scan the archives of data. The lists of processing_centers
    and file_types must be drawn from those defined for AWS in dynamodbinterface.py.

    The version must be a valid one, drawn from Versions.versions.

    The UCARprefix, ROMSAFprefix, JPLprefix, and EUMETSATprefix keys offer the option to override
    default paths regarding where to find UCAR, ROMSAF, JPL, and EUMETSAT input files. They
    can be found either on the local file system or in S3, in which case they
    should be prefixed with "s3://".

    If non-nominal occultation retrievals contributed by the ROM SAF should be
    included in the job definitions, set nonnominal to True.

    If AWS authentication is required in the current computing environment, it
    should be provided as a boto3 session. A logger generates output.

    Set liveupdate to True for liveupdate processing.

    The function returns a dictionary that completely describes where to find the
    input data files, subject to the function's restricting arguments, due for
    preprocessing. The returned dictionary contains two items:

        (1) "prefixes"
        (2) "jobs"

    The first of these keys, "prefixes", points to a dictionary that defines the
    prefixes of the input files. The keys of the "prefixes" dictionary are the
    requested processing_centers, and each contains a string that defines the input
    prefix for that processing center.  The second of these keys, "jobs", points
    to a list of dictionaries that define the directories containing the input
    files. Each dictionary in that list contains the following keys and contents:

        (a) date                A datetime.datetime instance of the date for RO data
        (b) mission             The RO mission (AWS name)
        (c) processing_center   The contributing processing center
        (d) file_type           The AWS RO file type ("level1b", "level2a", "level2b")
        (e) input_relative_dir  The directory in which the input files reside;
                                concatenate this with the absolute prefix of the
                                processing_center to establish an absolute path.
        (f) nfiles              The number of files in the directory that should
                                be preprocessed/translated.

    """

    #  Check input.

    for processing_center in processing_centers:
        if processing_center not in reformatters.keys():
            LOGGER.error(f'Processing center "{processing_center}" is not a valid processing center')
            #raise definejobsError( "InvalidInput", f'Processing center "{processing_center}" is not a valid processing center' )
            return 1

    for file_type in file_types:
        if file_type not in { f for center, reformatter in reformatters.items()
                    for f in reformatter.keys() }:
            LOGGER.error(f'File type "{file_type}" is not a valid file type')
            #raise definejobsError( "InvalidInput", f'File type "{file_type}" is not a valid file type' )
            return 1

    for mission in missions:
        if mission not in valid_missions['aws']:
            LOGGER.error(f'Mission "{mission}" is not a valid mission')
            #raise definejobsError( "InvalidInput", f'Mission "{mission}" is not a valid mission' )
            return 1

    #  Initialize s3fs instance.

    if session is None:
        create_session = True
    elif session.profile_name == "default":
        create_session = True
    else:
        create_session = False

    if create_session:
        s3 = s3fs.S3FileSystem( **( s3fsauth() ), client_kwargs={'region_name':session.region_name} )
    else:
        s3 = s3fs.S3FileSystem( profile=session.profile_name, client_kwargs={'region_name':session.region_name} )

    jobs = []

    #  Loop over date.

    date = datetime( year=daterange[0].year, month=daterange[0].month, day=daterange[0].day )

    while date <= daterange[1]:

        #  Get year, month, day, and day-of-year.

        year, month, day = date.year, date.month, date.day
        doy = ( date - datetime(year,1,1) ).days + 1

        for mission in missions:

            if "ucar" in processing_centers and mission in valid_missions['aws']:

                if UCARprefix is None:
                    if liveupdate:
                        UCARprefix = "s3://" + reformatters['ucar']['liveupdateBucket'] + "/untarred"
                    else:
                        UCARprefix = "s3://" + reformatters['ucar']['archiveBucket'] 

                for file_type in file_types:

                    if file_type == "level1b":
                        level = "level1b"
                    elif file_type in [ "level2a", "level2b" ]:
                        level = "level2"

                    #  Loop over UCAR mission paths. AWS mission to UCAR mission is not always a
                    #  one-to-one mapping, and so UCARmissionMapping is consulted.

                    sats = get_receiver_satellites( "aws", mission=mission )
                    UCARmissions = sorted( list( { sat['ucar']['mission'] for sat in sats } ) )

                    for UCARmission in UCARmissions:

                        #  What processing versions are available?
                        try:
                            processingVersions = s3.ls( os.path.join( UCARprefix, UCARmission ) )
                        except:
                            LOGGER.error( "*** " + os.path.join( UCARprefix, UCARmission ) + " does not exist." )
                            continue

                        for processingVersion in processingVersions:
                            # check we are in the right dir
                            subdir_list = s3.ls( processingVersion )
                            head, tail = os.path.split( subdir_list[0] )
                            if "level" not in tail:
                                subdirs = s3.ls( os.path.join( processingVersion, tail, level, f"{year:4d}", f"{doy:03d}" ) )
                            else:
                                try:
                                    subdirs = s3.ls( os.path.join( processingVersion, level, f"{year:4d}", f"{doy:03d}" ) )
                                except:
                                    LOGGER.info( "*** s3://" + \
                                        os.path.join( processingVersion, level, f"{year:4d}", f"{doy:03d}" ) + \
                                        " does not exist." )
                                    continue

                            type_subdirs = []

                            if file_type == "level1b":
                                for subdir in subdirs:
                                    head, tail = os.path.split( subdir )
                                    if re.search( "^atmPhs", tail ) or re.search( "^conPhs", tail ):
                                        type_subdirs.append( subdir )

                            elif file_type == "level2a":
                                for subdir in subdirs:
                                    head, tail = os.path.split( subdir )
                                    if re.search( "^atmPrf", tail ):
                                        type_subdirs.append( subdir )

                            elif file_type == "level2b":
                                for subdir in subdirs:
                                    head, tail = os.path.split( subdir )
                                    if re.search( "^wetPf2", tail ):
                                        type_subdirs.append( subdir )
                                    else:
                                        if re.search( "^wetPrf", tail ) and len(type_subdirs) == 0:
                                            type_subdirs.append( subdir )

                            if len( type_subdirs ) != 1:
                                LOGGER.info("type_subdirs !=1")
                                continue

                            #  Get a list of all files for this day and file type.

                            search_string = UCARprefix[5:] + "/(.*)$"
                            m = re.search( search_string, type_subdirs[0] )
                            path = m.group(1)
                            try:
                                filepaths = s3.ls( type_subdirs[0] )
                            except:
                                LOGGER.info( "*** s3://" + type_subdirs[0] + " does not exist." )
                                continue

                            #  Define the new set of jobs.

                            job = { 'file_type': file_type, 'processing_center': "ucar",
                                    'mission': mission, 'date': date.strftime("%Y-%m-%d"),
                                    'input_relative_dir': path, 'nfiles': len(filepaths) }
                            jobs.append( job )

                            LOGGER.info( json.dumps( job ) )

                            break


            if "romsaf" in processing_centers and mission in valid_missions['aws']:

                if ROMSAFprefix is None:
                    if liveupdate:
                        ROMSAFprefix = "s3://" + reformatters['romsaf']['liveupdateBucket'] + "/untarred"
                    else:
                        ROMSAFprefix = "s3://" + reformatters['romsaf']['archiveBucket'] 

                sats = get_receiver_satellites( "aws", mission=mission )
                ROMSAFmissions = sorted( list( { sat['romsaf']['mission'] for sat in sats } ) )

                #  Loop over ROMSAF missions.

                for ROMSAFmission in ROMSAFmissions:

                    try:
                        if liveupdate:
                            subdirs = s3.ls( os.path.join( ROMSAFprefix, ROMSAFmission, f"{year:4d}" ) )
                        else:
                            subdirs = s3.ls( os.path.join( ROMSAFprefix, "romsaf", "download", ROMSAFmission,
                                    f"{year:4d}" ) )
                    except:
                        if liveupdate:
                            LOGGER.info( "*** " + \
                                    os.path.join( ROMSAFprefix, ROMSAFmission, f"{year:4d}" ) + \
                                    " does not exist." )
                        else:
                            LOGGER.info( "*** " + \
                                    os.path.join( ROMSAFprefix, "romsaf", "download", ROMSAFmission, f"{year:4d}" ) + \
                                    " does not exist." )

                    for file_type in file_types:

                    #  Select subdirectories corresponding to "atm" or to "wet" files.

                        if file_type == "level2a":
                            search_string = f"^atm_{year:4d}{month:02d}{day:02d}"
                        elif file_type == "level2b":
                            search_string = f"^wet_{year:4d}{month:02d}{day:02d}"
                        else:
                            continue

                        type_subdirs = []
                        for subdir in subdirs:
                            head, tail = os.path.split( subdir )
                            if re.search( search_string, tail ):
                                type_subdirs.append( subdir )

                        if len( type_subdirs ) != 1:
                            LOGGER.info("type_subdirs != 1")
                            continue

                        #  Get a listing of all netcdf files for that mission/day.

                        fullpath = os.path.join( type_subdirs[0], f"{year:4d}-{month:02d}-{day:02d}" )
                        search_string = ROMSAFprefix[5:] + "/(.*)$"
                        m = re.search( search_string, fullpath )
                        path = m.group(1)

                        try:
                            paths = s3.ls( fullpath )
                        except:
                            LOGGER.info( "*** " + fullpath + " does not exist." )
                            continue

                        #  Define the new set of jobs.

                        filepaths = [ p for p in paths if re.search( "[._]nc$", p ) ]
                        nfilepaths = len( filepaths )

                        if nfilepaths > 0:

                            job = { 'date': date.isoformat()[:10], 'mission': mission, 'processing_center': "romsaf",
                                'file_type': file_type, 'input_relative_dir': path, 'nfiles': nfilepaths }
                            jobs.append( job )

                            LOGGER.info( json.dumps( job ) )

                        else:

                            LOGGER.info( f"No files found in s3://{fullpath}" )

                        #  Check for non-nominal subdirectory.

                        if nonnominal:

                            fullpath = os.path.join( fullpath, "non-nominal" )
                            search_string = ROMSAFprefix + "/(.*)$"
                            m = re.search( search_string, fullpath )
                            path = m.group(1)

                            try:
                                paths = s3.ls( fullpath )
                            except Exception as excpt:
                                LOGGER.error( fullpath + " does not exist." )
                                LOGGER.exception( json.dumps( excpt.args ) )
                                continue

                            filepaths = [ p for p in paths if re.search( "[._]nc$", p ) ]
                            nfilepaths = len( filepaths )

                            if nfilepaths > 0:

                                job = { 'date': date.isoformat()[:10], 'mission': mission, 'processing_center': "romsaf",
                                    'file_type': file_type, 'input_relative_dir': path, 'nfiles': nfilepaths }
                                jobs.append( job )

                                LOGGER.info( json.dumps( job ) )

                            else:

                                LOGGER.info( f"No files found in s3://{fullpath}" )

            if "jpl" in processing_centers and mission in valid_missions['aws']:

                if JPLprefix is None:
                    if liveupdate:
                        LOGGER.error( "no liveupdate option for jpl yet" )
                        break
                        #JPLprefix = "s3://" + reformatters['jpl']['liveupdateBucket'] 
                    else:
                        JPLprefix = "s3://" + reformatters['jpl']['archiveBucket'] 

                for file_type in file_types:

                    if file_type == "level1b":
                        jpl_file_type = "calibratedPhase"
                    elif file_type == "level2a":
                        jpl_file_type = "refractivityRetrieval"
                    elif file_type == "level2b":
                        jpl_file_type = "atmosphericRetrieval"
                    else:
                        LOGGER.error(f'File type "{file_type}" for processing center "jpl" is unrecognized.')
                        #raise definejobsError( "InvalidFiletype",f'File type "{file_type}" for processing center "jpl" is unrecognized.' )

                    path = os.path.sep.join( [ JPLprefix, mission, jpl_file_type,
                        "{:4d}/{:02d}/{:02d}".format( date.year, date.month, date.day ) ] )

                    if JPLprefix[:5] == "s3://":
                        try:
                            files = s3.ls( path )
                        except:
                            LOGGER.info( "*** " + path + " does not exist." )
                            continue

                    else:
                        if not os.path.isdir( path ):
                            LOGGER.info( "*** " + path + " does not exist." )
                            continue
                        files = os.listdir( path )

                    files = [ f for f in files if re.search( "[._]nc$", f ) ]
                    nfiles = len( files )

                    if nfiles > 0:

                        if JPLprefix[:5] == "s3://":
                            path_split = re.split( "/", path[5:] )
                            prefix_split = re.split( "/", JPLprefix[5:] )
                        else:
                            path_split = re.split( "/", path )
                            prefix_split = re.split( "/", JPLprefix )

                        input_relative_dir = "/".join( path_split[len(prefix_split):] )

                        job = { 'date': date.isoformat()[:10], 'mission': mission, 'processing_center': "jpl",
                                'file_type': jpl_file_type, 'input_relative_dir': input_relative_dir, 'nfiles': nfiles }
                        jobs.append( job )

                        LOGGER.info( json.dumps( job ) )

                    else:

                        LOGGER.info( f"*** No files found in {path}" )

            if "eumetsat" in processing_centers and mission in valid_missions['aws']:

                if EUMETSATprefix is None:
                    if liveupdate:
                        EUMETSATprefix = "s3://" + reformatters['eumetsat']['liveupdateBucket'] + "/untarred"
                    else:
                        LOGGER.error( "only liveupdate option for eumetstat" )
                        break

                for file_type in file_types:

                    if file_type == "level1b":
                        level = "level1b"
                    else:
                        LOGGER.error( "only level1b exists for eumetstat" )
                        break

                    #  Loop over UCAR mission paths. AWS mission to UCAR mission is not always a
                    #  one-to-one mapping, and so UCARmissionMapping is consulted.

                    sats = get_receiver_satellites( "aws", mission = mission )
                    EUMETSATmissions = sorted( list( { sat['eumetsat']['mission'] for sat in sats } ) )
                    for EUMETSATmission in EUMETSATmissions:
                        #  What processing versions are available?
                        try:
                            processingVersions = s3.ls( os.path.join( EUMETSATprefix, EUMETSATmission ) )
                        except:
                            LOGGER.error( "*** " + os.path.join( EUMETSATprefix, EUMETSATmission ) + " does not exist." )
                            continue

                        for processingVersion in processingVersions:
                            try:
                                subdirs = s3.ls( os.path.join( processingVersion, level, f"{year:4d}", f"{doy:03d}" ) )
                            except:
                                LOGGER.info( "*** s3://" + \
                                    os.path.join( processingVersion, level, f"{year:4d}", f"{doy:03d}" ) + \
                                    " does not exist." )
                                continue

                            type_subdirs = []

                            for subdir in subdirs:
                                head, tail = os.path.split( subdir )
                                #go in every subdir as they have diff prefixes
                                type_subdirs.append( subdir )

                            if len( type_subdirs ) != 1:
                                LOGGER.info("type_subdirs !=1")
                                continue

                            #  Get a list of all files for this day and file type.
                            search_string = EUMETSATprefix[5:] + "/(.*)$"
                            m = re.search( search_string, type_subdirs[0] )
                            path = m.group(1)
                            try:
                                filepaths = s3.ls( type_subdirs[0] )
                            except:
                                LOGGER.info( "*** s3://" + type_subdirs[0] + " does not exist." )
                                continue

                            #  Define the new set of jobs.

                            job = { 'file_type': file_type, 'processing_center': "eumetsat",
                                    'mission': mission, 'date': date.strftime("%Y-%m-%d"),
                                    'input_relative_dir': path, 'nfiles': len(filepaths) }

                            jobs.append( job )

                            LOGGER.info( json.dumps( job ) )

        #  Next day.

        date = date + timedelta(days=1)

    #  Define returned dictionary.

    prefixes = {}

    if "ucar" in processing_centers:
        prefixes.update( { 'ucar': UCARprefix } )
    if "romsaf" in processing_centers:
        prefixes.update( { 'romsaf': ROMSAFprefix } )
    if "jpl" in processing_centers:
        prefixes.update( { 'jpl': JPLprefix } )
    if "eumetsat" in processing_centers:
        prefixes.update( { 'eumetsat': EUMETSATprefix } )

    ret = { 'prefixes': prefixes, 'jobs': jobs }

    LOGGER.info( "prefixes={:}, njobs={:}".format( json.dumps( ret['prefixes'] ), len( ret['jobs'] ) ) )

    return ret


################################################################################
#  createjobdefinitionsjson
################################################################################

def createjobdefinitionsjson( daterange, missions, processing_centers, file_types, version,
        jsonfile, UCARprefix=None, ROMSAFprefix=None, JPLprefix=None, EUMETSATprefix=None,
        session=None ):
    """This is a wrapper to definejobs that converts its output to a JSON file. The
    daterange should be a string containing to dates of the form "YYYY-MM-DD" separated
    by a space. The first should be the first day over which to scan; the second should
    be the last day of the scan. missions should be a list of AWS missions over which
    to scan for input files. processing_centers should be a list of contributing
    processing centers to consider. file_types should be a list of AWS file types
    ("level1b", "level2a", etc.) to search over. The version should be one element of
    the Versions.versions list. Output is sent to JSON file jsonfile.

    The keys UCARprefix, ROMSAFprefix, JPLprefix offer override options regarding where
    UCAR, ROMSAF and JPL input files can be found. They can be defined as on the local
    file system or in an S3 bucket, in which case they should be prefixed with
    "s3://".

    The session is a predefined AWS session, authenticated as needed."""

    with open( jsonfile, 'w' ) as out:

        job_definitions = definejobs( daterange, missions, processing_centers, file_types, version,
            UCARprefix=UCARprefix, ROMSAFprefix=ROMSAFprefix, JPLprefix=JPLprefix,
            EUMETSATprefix=EUMETSATprefix, session=session )

        LOGGER.info( f"Writing to JSON file {jsonfile}." )

        json.dump( job_definitions, out, indent="  " )

    return


################################################################################
#  A next job iterator for use in DASK.
################################################################################

class DASKjobs():
    """Create an iterable of DASK preprocessing jobs based on the contents
    of a JSON file created by createjobdefinitionsjson and returned by
    json.load. It will also work with the output of definejobs. Each next
    step in the iterable responds with a 4-tuple of

    ( file_type, processing_center, input_root_path, input_relative_path )

    """

    def __init__( self, job_definitions, session=None ):

        #  Check input.

        if not isinstance( job_definitions, dict ):
            comment = "job_definitions is not a dictionary"
            LOGGER.error( comment )
            raise DASKjobsError( "InvalidJobDefinitions", comment )

        for key in [ 'prefixes', 'jobs' ]:
            if key not in set( job_definitions.keys() ):
                comment = f"job_definitions does not have key {key}"
                LOGGER.error( comment )
                raise DASKjobsError( "InvalidJobDefinitionsKeys", comment )

        if not isinstance( job_definitions['jobs'], list ):
            comment = "job_definitions['jobs'] is not a list"
            LOGGER.error( comment )
            raise DASKjobsError( "InvalidJobDefinitionsJobs", comment )

        if len( job_definitions['jobs'] ) == 0:
            comment = "no jobs in job_definitions['jobs']"
            LOGGER.error( comment )
            raise DASKjobsError( "NoJobDefinitionsJobs", comment )

        #  Establish s3fs.

        if session is None:
            create_session = True
        elif session.profile_name == "default":
            create_session = True
        else:
            create_session = False

        if create_session:
            self.s3 = s3fs.S3FileSystem( **( s3fsauth() ), client_kwargs={'region_name':session.region_name} )
        else:
            self.s3 = s3fs.S3FileSystem( profile=session.profile_name, client_kwargs={'region_name':session.region_name} )

        self.jobs = copy.deepcopy( job_definitions['jobs'] )
        self.prefixes = copy.deepcopy( job_definitions['prefixes'] )
        self.inputfiles = self.loadfiles( self.jobs[0] )

    def __iter__( self ):

        return self

    def loadfiles( self, job ):

        #  Load all files corresponding to a particular job definition.

        file_type, processing_center = job['file_type'], job['processing_center']
        input_root_path = self.prefixes[ processing_center ]
        input_path = os.path.join( input_root_path, job['input_relative_dir'] )
        files = self.s3.ls( input_path )

        inputfiles = [ f for f in files if re.search( "[._]nc$", f ) ]

        if len( inputfiles ) == 0:
            inputfiles = [ f for f in files if re.search( "[._]nc$", f ) ]

        if len( inputfiles ) == 0:
            comment = f"No files in s3://{input_path}"
            LOGGER.error( comment )
            #raise DASKjobsError( "NoInputFiles", comment )

        return inputfiles

    def __next__( self ):

        #  Identify the job. Increment to next job if necessary.
        if len( self.inputfiles ) == 0:

            #  Get rid of previous job.

            last_job = self.jobs.pop( 0 )

            if len( self.jobs ) == 0:
                raise StopIteration

            #  Load input files for the next job.

            self.inputfiles = self.loadfiles( self.jobs[0] )

        #  Identify the current job.

        job = self.jobs[0]

        #  Execute jobs.

        file_type, processing_center = job['file_type'], job['processing_center']
        input_root_path = self.prefixes[processing_center]

        #  Next input file.

        inputfile = self.inputfiles[0]

        #  Identify the input.

        pathsplit = inputfile.split( os.path.sep )
        rootpathsplit = input_root_path[5:].split( os.path.sep )
        input_relative_path = os.path.sep.join( pathsplit[len(rootpathsplit):] )

        #  Define the return tuple.

        ret = file_type, processing_center, input_root_path, input_relative_path

        #  Get rid of input file.

        last_file = self.inputfiles.pop(0)

        return ret




def define_batchprocess_jobs( job_definitions, template, jobsperfile, session ):
    """Generate batchprocess input JSON files based on the job definitions
    generated by Utilities.definejobs.definejobs. The arguments are

    job_definitions     The job definitions created by
                        Utilities.definejobs.definejobs.
    template            A string that defines the template for the
                        output JSON files that can be sent to batchprocess.
                        The template will be subjected to its format
                        method with one argument: an integer index. If it
                        is prefixed with s3, it is interpreted to write to
                        an S3 bucket.
    jobsperfile         The maximum number of ProcessReformat jobs
                        in each output JSON file.
    session             A boto3 session, used in case authentication is
                        is needed."""

    #  Initialize.

    njobs = 0
    nfiles = 0
    last_input_root_path = None
    last_processing_center = None

    #  Iterate over jobs.

    myDASKjobs = DASKjobs( job_definitions, session=session )
    myIter = iter( myDASKjobs )

    for file_type, processing_center, input_root_path, input_relative_path in myIter:

        #  Check input.

        if last_input_root_path is not None:
            if input_root_path != last_input_root_path:
                LOGGER.error( f"Last input_root_path ({last_input_root_path}) " + \
                    f"must be the same as the new input_root_path ({input_root_path})" )
                return

        if last_processing_center is not None:
            if processing_center != last_processing_center:
                LOGGER.error( f"Last processing_center ({last_processing_center}) " + \
                    f"must be the same as the new processing_center ({processing_center})" )
                return

        last_input_root_path = copy.copy( input_root_path )
        last_processing_center = copy.copy( processing_center )

        #  Initiate a new output structure?

        if njobs % jobsperfile == 0:
            out = { 'InputPrefix': input_root_path, 'ProcessingCenter': processing_center, 'InputFiles': [] }

        #  Add another input file.

        out['InputFiles'].append( input_relative_path )

        #  Add to njobs.

        njobs += 1

        #  Write to output.

        if njobs % jobsperfile == 0:
            nfiles += 1
            outputJSONfile = template.format( nfiles )
            writeOutputJSONfile( out, outputJSONfile, session=session )

    if len( out['InputFiles'] ) > 0:
        nfiles += 1
        outputJSONfile = template.format( nfiles )
        writeOutputJSONfile( out, outputJSONfile, session=session )

    return


def writeOutputJSONfile( output_structure, output_file, session=None ):

    LOGGER.info( f"Writing to {output_file}" )

    if output_file[:5] == "s3://":

        if session is None:
            LOGGER.error( "Session needs to be defined for access to S3" )
            return "fail"
        else:
            s3 = session.client( "s3" )
            head, tail = os.path.split( output_file[5:] )

            #  Write file locally.

            with open( tail, 'w' ) as out:
                json.dump( output_structure, out, indent="    " )

            #  Upload to S3 bucket.

            file_split = re.split( "/", output_file[5:] )
            bucketName, bucketPath = file_split[0], "/".join( file_split[1:] )
            s3.upload_file( tail, bucketName, bucketPath )

            #  Remove local file.

            os.unlink( tail )

    else:

        with open( output_file, 'w' ) as out:
            json.dump( output_structure, out, indent="    " )

    return output_file

def find_mission_year_range(version,mission,processing_center,liveupdate):
    year_list = []
    s3fs_client = s3fs.S3FileSystem( **( s3fsauth() ), client_kwargs={'region_name':default_region} )
    if liveupdate:
        center_prefix = "s3://" + reformatters[processing_center]['liveupdateBucket'] 
        if processing_center in [ "ucar", "romsaf", "eumetsat" ]: 
            center_prefix += "/untarred"
        elif processing_center == "jpl":
            print("jpl has no liveupdate yet")
            exit(0)            
    else:
        center_prefix = "s3://" + reformatters[processing_center]['archiveBucket']
        if processing_center in [ 'romsaf' ]: 
            center_prefix += "/romsaf/download"
        elif processing_center in [ 'ucar', 'jpl' ]: 
            pass
        elif processing_center == "eumetsat":
            print("eumetsat is liveupdate only")
            exit(0)

    if processing_center == "romsaf":
        #diff folder structure
        if mission == "cosmic1":
            mission = "cosmic" #only for archived
        print(os.path.join(center_prefix, mission))
        subdirs = s3fs_client.ls( os.path.join(center_prefix, mission))
        for subdir in subdirs:
            head,tail = os.path.split(subdir)
            year_list.append(tail)
     
    elif processing_center == "jpl":
        #diff folder structure
        subdirs = s3fs_client.ls( os.path.join(center_prefix, mission,"calibratedPhase"))
        for subdir in subdirs:
            head,tail = os.path.split(subdir)
            year_list.append(tail)
    else:         
        if mission == "metop":
            #loop to get all years
            receiver_list  = [ "metopa", "metopb", "metopc" ]
        elif mission == "gpsmet":
            receiver_list  = [ "gpsmet", "gpsmetas"]
        else:
            receiver_list = [mission]

        for receiver in receiver_list:
            processingVersions = s3fs_client.ls( os.path.join( center_prefix, receiver ) )

            for processingVersion in processingVersions:
                for level in ["level1b","level2"]:
                    if level == "level2" and processing_center == "eumetsat": continue
                    if mission == "geoopt" and liveupdate:
                        subdirs = s3fs_client.ls( os.path.join( processingVersion, "postProc", level) )
                    elif mission == "spire" and liveupdate:
                        subdirs = s3fs_client.ls( os.path.join( processingVersion, "nrt", level) )
                    elif mission == "planetiq" and liveupdate:
                        subdirs = s3fs_client.ls( os.path.join( processingVersion, "nrt", level) )
                    else:
                        subdirs = s3fs_client.ls( os.path.join( processingVersion, level) )

                    for subdir in subdirs:
                        head,tail = os.path.split(subdir)
                        year_list.append(tail)

    return sorted(list(set(year_list)))


################################################################################
#  Main program.
################################################################################

def main(): 

    #  Defaults.

    jobsperfile = 3000
    default_AWSversion = "1.1"

    all_filetypes = []
    for key, val in reformatters.items(): 
        all_filetypes += [ field for field in val.keys() if re.search(r"^level",field) ]
    sorted_valid_filetypes = sorted( list( set( all_filetypes ) ) )

    #  Create the argument parser.

    parser = argparse.ArgumentParser()

    parser.add_argument( "processing_center", help="The contributing AWS processing center; valid processing centers include: " + \
            ", ".join( [ f'"{v}"' for v in sorted(list(reformatters.keys())) ] ) + "." )

    parser.add_argument( "mission", help="RO mission for ingest, AWS naming convention; valid missions include: " + \
            ", ".join( [ f'"{v}"' for v in valid_missions['aws'] ] ) + "." )

    parser.add_argument( "file_type", help="The file type or processing level; valid file types include: " + \
            ", ".join( [ f'"{v}"' for v in sorted_valid_filetypes ] ) + "." )

    parser.add_argument( "--version", dest='AWSversion', type=str, default=default_AWSversion,
        help=f'The output format version. The default is AWS version "{default_AWSversion}". ' + \
        'The valid versions are ' + ', '.join( [ f'"{v}"' for v in valid_versions ] ) + "." ) 

    parser.add_argument( "--liveupdate", default=False, action='store_true',
        help="Require that the incoming data from the liveupdate buckets" )

    parser.add_argument( "--daterange", default=None,
        help="Set a time window over which to search for jobs by writing isoformat " + \
        "datetimes for the first and last day, separated by colon only, within " + \
        "a single string; for example, 2009-02-01:2009-02-28 to consider only the " + \
        "month of February, 2009." )

    parser.add_argument( "--jobsperfile", default=jobsperfile, type=bool,
        help="Number of jobs to include in an output bachprocess file; the default " + \
        "is {:}.".format( jobsperfile ) )

    parser.add_argument( "--nonnominal", default=False, action='store_const',
        const=True, help="Use to include non-nominal occultation retrievals contributed " + \
        "by the ROM SAF in the output job definitions. The default is that they not be " + \
        "included." )

    parser.add_argument( "--verbose", dest='verbose', action='store_true',
            help='If verbose is set, then all logger output is directed to output. By default ' +
            'only warnings, errors, and exceptions are written to the output logs.' )

    #  Parse.

    args = parser.parse_args()

    #  Version module.

    version = get_version( args.AWSversion )
    if version is None:
        print( f'AWS version "{args.AWSversion}" is unrecognized.' )
        exit(-1)

    loggingRoot = version['module'].loggingRoot
    definitionsBucket = version['module'].definitionsBucket
    #  Define the session for authentication purposes.

    session = boto3.session.Session( region_name=default_region )

    #  Liveupdate processing.

    liveupdate = bool( args.liveupdate )

    if liveupdate:
        logging_output_base = "createjobs.{:}-{:}-{:}_liveupdate".format(
                args.processing_center, args.mission, args.file_type)
        template = "s3://{:}/batchprocess-jobs/{:}-{:}-{:}".format(
                definitionsBucket, args.processing_center,
                args.mission, args.file_type ) \
                + ".{:06d}_liveupdate.json"
    else:
        logging_output_base = "createjobs.{:}-{:}-{:}".format(
                args.processing_center, args.mission, args.file_type)
        template = "s3://{:}/batchprocess-jobs/{:}-{:}-{:}".format(
                definitionsBucket, args.processing_center,
                args.mission, args.file_type ) \
                + ".{:06d}.json"

    #  Define logger output.
    error_logging_file = logging_output_base + ".errors.log"
    warning_logging_file = logging_output_base + ".warnings.log"

    handlers = []
    formatter = logging.Formatter('%(pathname)s:%(lineno)d %(levelname)s: %(message)s')

    e_filehandle = logging.FileHandler( filename=error_logging_file )
    e_filehandle.setLevel( "ERROR" )
    e_filehandle.setFormatter( formatter )
    handlers.append(e_filehandle)
    print( f"Logging errors and exceptions to {error_logging_file}." )

    w_filehandle = logging.FileHandler( filename=warning_logging_file )
    w_filehandle.setLevel( "WARNING" )
    w_filehandle.setFormatter( formatter )
    handlers.append(w_filehandle)
    print( f"Logging warnings, errors and exceptions to {warning_logging_file}." )

    if args.verbose:
        i_streamhandle = logging.StreamHandler( sys.stdout )
        i_streamhandle.setLevel( "INFO" )
        i_streamhandle.setFormatter( formatter )
        handlers.append(i_streamhandle)
        print( f"Logging information, warnings, errors and exceptions to standard output." )
    else:
        i_streamhandle = logging.StreamHandler( sys.stdout )
        i_streamhandle.setLevel( "WARNING" )
        i_streamhandle.setFormatter( formatter )
        handlers.append(i_streamhandle)
        print( f"Logging warnings, errors and exceptions to standard output." )

    for h in handlers:
        LOGGER.addHandler(h)

    #  Define date range
    year_list = []
    if args.daterange is None:
        #checks bucket for list of years so we don't have to lop through all of them
        m_str = find_mission_year_range(version,args.mission,args.processing_center,args.liveupdate)
        daterange = [ datetime(int(m_str[0]),1,1), datetime(int(m_str[-1]),12,31) ]

    else:
        m = args.daterange.split(':')
        try:
            daterange = [ datetime.strptime( m[0],"%Y-%m-%d" ), datetime.strptime( m[1],"%Y-%m-%d" ) ]
        except:
            LOGGER.error( "Argument to date range '{:}' cannot be interpreted ".format( str(args.daterange) ) + \
                    "as two datetimes yyyy-mm-dd separated by a colon." )
            exit(-2)

    LOGGER.info("daterange",daterange)
    jobs = definejobs( daterange, [args.mission], [args.processing_center], [args.file_type],
            version, nonnominal=bool(args.nonnominal), session=session, liveupdate=liveupdate )

    #  Send jobs to define_batchprocess_jobs.

    if jobs == 1 or len(jobs['jobs']) == 0:
        LOGGER.warning( f"No files were found for {args.processing_center}-{args.mission}" )
    else:
        define_batchprocess_jobs( jobs, template, args.jobsperfile, session )

    #  Upload log files if they have content.

    if os.path.getsize(error_logging_file) > 0 :
        s3 = session.client( service_name='s3', region_name=default_region )
        upload_path = os.path.join( loggingRoot, "errors", error_logging_file )
        print( 'Uploading error log to "s3://{:}".'.format( os.path.join( definitionsBucket, upload_path ) ) )
        s3.upload_file( error_logging_file, definitionsBucket, upload_path )

    if os.path.getsize(warning_logging_file) > 0 :
        s3 = session.client( service_name='s3', region_name=default_region )
        upload_path = os.path.join( loggingRoot, "warnings", warning_logging_file )
        print( 'Uploading warning log to "s3://{:}".'.format( os.path.join( definitionsBucket, upload_path ) ) )
        s3.upload_file( warning_logging_file, definitionsBucket, upload_path )


if __name__ == "__main__":
    main()
    pass


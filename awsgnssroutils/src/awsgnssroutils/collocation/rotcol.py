import argparse
import re
import json
from getpass import getpass 
from datetime import datetime
from time import time

from awsgnssroutils.database import valid_table 
from awsgnssroutils.collocation.instruments import instruments

def execute_rotation_collocation( missions, datetimerange, ro_processing_center, 
        nadir_instrument, nadir_satellite, outputfile, nodata=False ): 
    """Execute a rotation-collocation computation. 

    Arguments
    =========

    missions        A string, tuple, or list of GNSS RO missions to consider. 

    datetimerange   A 2-element tuple or list of times, both ISO format strings, 
                    defining the time bounds for the RO soundings. 

    ro_processing_center
                    The RO processing center to be used as the source for RO 
                    retrieval data. 

    nadir_instrument
                    The name of the nadir-scanning radiance sounding instrument 
                    whose soundings are sought for collocation with RO soundings. 

    nadir_satellite
                    The name of the satellite that hosts the nadir-scanning 
                    instrument. 

    outputfile      The name of the NetCDF output file generated by the 
                    execution. 

    nodata          When true, do not extract observational data for the 
                    collocated RO and nadir-scanner soundings. 
    """

    #  Initialize return structure. 

    ret = { 'status': None, 'messages': [], 'comments': [] }

    #  Relevant imports. 

    from awsgnssroutils.database import RODatabaseClient
    from awsgnssroutils.collocation.core.spacetrack import Spacetrack
    from awsgnssroutils.collocation.core.spacetrack import checkdefaults as spacetrack_checkdefaults
    from awsgnssroutils.collocation.core.rotation_collocation import rotation_collocation

    #  Initialize RO database client and access to Space-Track data. 

    ret = spacetrack_checkdefaults()
    if ret['status'] == "fail": 
        print( "Error: \n" + "\n".join( ret['comments'] ) )
        return

    rodb = RODatabaseClient()
    strack = Spacetrack()

    #  Instantiate the nadir-scanner instrument. 

    from awsgnssroutils.collocation.core.nasa_earthdata import Satellites as NASA_Satellites
    from awsgnssroutils.collocation.core.eumetsat import metop_satellites as EUMETSAT_Satellites

    if nadir_satellite in NASA_Satellites.keys(): 

        from awsgnssroutils.collocation.core.nasa_earthdata import checkdefaults, NASAEarthdata
        ret = checkdefaults()
        if ret['status'] == "fail": 
            print( "Error: \n" + "\n".join( ret['comments'] ) )
            return

        data_center = "NASA Earthdata" 
        nasa_earthdata_access = NASAEarthdata()
        inst = instruments[nadir_instrument]['class']( nadir_satellite, nasa_earthdata_access, spacetrack=strack )

    elif nadir_satellite in EUMETSAT_Satellites: 

        from awsgnssroutils.collocation.core.eumetsat import checkdefaults, EUMETSATDataStore
        ret = checkdefaults()
        if ret['status'] == "fail": 
            print( "Error: \n" + "\n".join( ret['comments'] ) )
            return

        data_center = "EUMETSAT Data Store"
        eumetsat_data_store = EUMETSATDataStore()
        inst = instruments[nadir_instrument]['class']( nadir_satellite, eumetsat_data_store, spacetrack=strack )

    #  Collocation tolerances. 

    time_tolerance = 600                           # 10 min/600 sec
    spatial_tolerance = 150.0e3                    # m

    #  Get occultation geolocations. 

    print( "Querying occultation database" )

    tbegin = time()
    occs = rodb.query( missions=missions, datetimerange=[ dt.isoformat() for dt in datetimerange ], 
                availablefiletypes=f'{ro_processing_center}_refractivityRetrieval', silent=True )
    tend = time()

    if occs.size == 0: 
        ret['status'] = "fail"
        ret['messages'].append( "NoOccultationData" )
        ret['comments'].append( "No occultation data found for missions and time range" )
        return ret

    print( "  - number found = {:}".format( occs.size ) )
    print( "  - elapsed time = {:10.3f} s".format( tend-tbegin ) )

    #  Exercise rotation-collocation. 

    print( "Executing rotation-collocation" )

    tbegin = time()
    ret_rotation = rotation_collocation( inst, occs, time_tolerance, spatial_tolerance, 2 )
    tend = time()

    ret['messages'] += ret_rotation['messages']
    ret['comments'] += ret_rotation['comments']

    if ret_rotation['status'] == "fail": 
        ret['status'] = "fail"
        print()
        return ret

    collocations_rotation = ret_rotation['data']

    print( "  - number found = {:}".format( len( collocations_rotation ) ) )
    print( "  - elapsed time = {:10.3f} s".format( tend-tbegin ) )

    if not nodata: 

        #  Populate instrument data. 

        print( f'Downloading instrument data from {data_center}' )
        tbegin = time()
        ret_populate = inst.populate( datetimerange )
        tend = time()

        print( "  - elapsed time = {:10.3f} s".format( tend-tbegin ) )

        #  Extract data. 

        print( "Extracting collocation data" )

        tbegin = time()
        for collocation in collocations_rotation: 
            occid = collocation.get_data( ro_processing_center )
        tend = time()

        print( "  - elapsed time = {:10.3f} s".format( tend-tbegin ) )

    #  Save to output file. 

    tbegin = time()
    print( f"Writing to output file {outputfile}" )
    collocations_rotation.write_to_netcdf( outputfile )
    tend = time()

    print( "  - elapsed time = {:10.3f} s".format( tend-tbegin ) )

    #  Done. 

    ret['status'] = "success" 

    return ret


def main(): 

    #  Choices and defaults. 

    version = "v1.1"
    output_default = "output.nc"
    ro_processing_center_default = "ucar"

    all_valid_missions = sorted( list( set( [ item['mission'] for item in valid_table 
            if item['version']==version and item['filetype'] == "refractivityRetrieval" ] ) ) )

    all_valid_centers = sorted( list( set( [ item['center'] for item in valid_table 
            if item['version']==version and item['filetype'] == "refractivityRetrieval" ] ) ) )

    all_valid_instruments = sorted( list( instruments.keys() ) )

    all_valid_satellites = []
    for key, val in instruments.items(): 
        all_valid_satellites += val['valid_satellites']
    all_valid_satellites = sorted( list( set( all_valid_satellites ) ) )

    #  Define the parent (root) parser. 

    parser = argparse.ArgumentParser( prog='rotcol', add_help=True, 
            description="""This program performs a collocation-finding calculation by between 
            GNSS radio occultation data and passive nadir-scanner sounding data by implementing 
            the rotation-collocation algorithm, generating NetCDF output containing the 
            data associated with the collocations. The user must set various defaults 
            (setdefaults) before executing (execute) a rotation-collocation computation.""", 
            epilog=" " )

    subparsers = parser.add_subparsers( dest="command" )

    #  Define the setdefaults parser. 

    setdefaults_parser = subparsers.add_parser( "setdefaults", 
            help="""Set default paths for data and metadata, and define login fields 
            (username, password, etc.) for access to various services. The services are 
            "awsro", "eumetsat", "earthdata", and "spacetrack".""" )

    setdefaults_parser.add_argument( "service", type=str, 
            help=
            """The service for which to set defaults. 

            (1) "awsro" refers to the AWS Registry of Open Data repository for GNSS radio 
            occultation data. If requested, the user should provide the dataroot and the 
            metadataroot paths where the data and RO metadata should be stored on the local 
            file system.

            (2) "eumetsat" refers to the EUMETSAT Data Store. If requested, the user 
            should consider entering the dataroot where EUMETSAT Data Store should be stored 
            on the local file system; also, the ConsumerKey and SecretKey that provides online 
            access to the EUMETSAT Data Store, which can be obtained from the EUMETSAT Data 
            Store website. 

            (3) "earthdata" refers to the NASA Earthdata portal to the NASA DAACs. If 
            requested, the user should consider entering the dataroot where Earthdata data 
            should be stored on the local file system; also the username and password for the 
            user's account in NASA Earthdata.

            (4) "spacetrack" refers to the Space-Track portal for orbital TLE data. If 
            requested, the user should consider entering the dataroot where Space-Track TLE 
            data should be stored on the local file system; also, the username and password 
            for the user's Space-Track account.""" ) 

    setdefaults_parser.add_argument( "--dataroot", dest="dataroot", default=None, type=str, required=False, 
            help="""Path on the local file system where data should be downloaded and stored 
            for future reference (and efficiency).""" )

    setdefaults_parser.add_argument( "--metadataroot", dest="metadataroot", default=None, type=str, required=False, 
            help="""Path on the local file system where metadata should be downloaded and stored 
            for future reference (and efficiency).""" )

    setdefaults_parser.add_argument( "--username", dest="username", default=None, type=str, required=False, 
            help="""Username for the Earthdata or Space-Track account; user will be prompted for password.""" )

    setdefaults_parser.add_argument( "--consumer-key", dest="consumer_key", default=None, type=str, required=False, 
            help="""ConsumerKey as provided by the EUMETSAT Data Store.""" )

    setdefaults_parser.add_argument( "--consumer-secret", dest="consumer_secret", default=None, type=str, required=False, 
            help="""ConsumerSecret as provided by the EUMETSAT Data Store.""" )

    #  Define the execution parser. 

    collocation_parser = subparsers.add_parser( "execute", 
            help="""Execute a rotation-collocation calculation to find 
            collocations between GNSS radio occultation soundings and a selected 
            nadir-scanning instrument. In this the user must list the RO missions 
            from which to draw data, what nadir-scanning instrument on what satellite 
            to search for collocated sounder data, and over what time period 
            collocations are being sought.""" )

    missions_default = "cosmic1 cosmic2 metop"
    collocation_parser.add_argument( "missions", type=str, default=missions_default, 
            help="""A list of GNSS radio occultation missions to draw from, separated by white space. Choices 
            are """ + ", ".join( all_valid_missions ) + f'.  The default is "{missions_default}".' )

    collocation_parser.add_argument( "timerange", type=str, 
            help="""Two ISO-format UTC datetimes defining the time interval over which to search for 
            collocations, separated by spaces""" )

    collocation_parser.add_argument( "nadir_instrument", type=str, 
            help="""Name of the nadir-scanning instrument. Choices are """ + \
                    ', '.join( all_valid_instruments ) + "." )

    collocation_parser.add_argument( "nadir_satellite", type=str, 
            help="""Name of the satellite hosting the nadir-scanning instrument. Choices are """ + \
                    ', '.join( all_valid_satellites ) + "." )

    collocation_parser.add_argument( "--nodata", default=False, action="store_true", 
            help="""Write geolocation information on collocation, but do not extract any RO profile or 
                    nadir-scanning instrument data. The default is to extract and write sounding data 
                    into the output file.""" )

    collocation_parser.add_argument( "--ro-center", dest="ro_processing_center", type=str, 
            choices=all_valid_centers, default=ro_processing_center_default, 
            help='Name of the RO processing center from which to take data; by default ' + \
                    f'"{ro_processing_center_default}".' )

    collocation_parser.add_argument( "--output", "-o", type=str, default=output_default, 
            help=f'Output file name for collocation data; by default "{output_default}".' )

    #  Parse arguments. 

    args = parser.parse_args()

    if args.command == "setdefaults": 

        #  Set defaults. 

        if args.service == "awsro": 

            from awsgnssroutils.database import setdefaults

            kwargs = { 'version': "v1.1" }
            if args.dataroot is not None: 
                kwargs.update( { 'data_root': args.dataroot } )
            if args.metadataroot is not None: 
                kwargs.update( { 'metadata_root': args.dataroot } )

            print( "Updating AWS RO defaults: " + json.dumps( kwargs ) )
            setdefaults( **kwargs )

        elif args.service == "eumetsat": 

            from awsgnssroutils.collocation.core.eumetsat import setdefaults

            kwargs = {}
            if args.dataroot is not None: 
                kwargs.update( { 'root_path': args.dataroot } )
            if args.consumer_key is not None and args.consumer_secret is not None: 
                kwargs.update( { 'eumetsattokens': ( args.consumer_key, args.consumer_secret ) } )

            print( "Updating EUMETSAT defaults: " + json.dumps( kwargs ) )
            ret = setdefaults( **kwargs )
            if ret['status'] == "fail": 
                print( "Error: \n" + "\n".join( ret['comments'] ) )

        elif args.service == "earthdata": 

            from awsgnssroutils.collocation.core.nasa_earthdata import setdefaults

            kwargs = {}
            if args.dataroot is not None: 
                kwargs.update( { 'root_path': args.dataroot } )
            if args.username is not None: 
                password = getpass()
                kwargs.update( { 'earthdatalogin': ( args.username, password ) } )

            print( "Updating NASA Earthdata defaults: " + json.dumps( kwargs ) )
            ret = setdefaults( **kwargs )
            if ret['status'] == "fail": 
                print( "Error: \n" + "\n".join( ret['comments'] ) )

        elif args.service == "spacetrack": 

            from awsgnssroutils.collocation.core.spacetrack import setdefaults

            kwargs = {}
            if args.dataroot is not None: 
                kwargs.update( { 'root_path': args.dataroot } )
            if args.username is not None: 
                password = getpass()
                kwargs.update( { 'spacetracklogin': ( args.username, password ) } )

            print( "Updating Space-Track defaults: " + json.dumps( kwargs ) )
            ret = setdefaults( **kwargs )
            if ret['status'] == "fail": 
                print( "Error: \n" + "\n".join( ret['comments'] ) )

        else: 

            print( f'Invalid service: "{args.service}"' )

    elif args.command == "execute": 

        #  Execute rotation-collocation algorithm for collocation finding. 

        #  Get arguments for rotation-collocation: missions, timerange, nadir_satellite, 
        #  nadir_instrument, ro_processing_center. 

        missions = re.split( "\s+", args.missions )

        ss = re.split( "\s+", args.timerange )
        timerange = ( datetime.fromisoformat( ss[0] ), datetime.fromisoformat( ss[1] ) )

        nadir_satellite = str( args.nadir_satellite )
        nadir_instrument = str( args.nadir_instrument )
        ro_processing_center = str( args.ro_processing_center )
        output_file = str( args.output )

        #  Check for valid satellite-instrument combination. 

        if nadir_instrument not in instruments.keys(): 

            print( f'Instrument "{nadir_instrument}" is unrecognized. Valid satellite ' + \
                    'instruments are ' + ", ".join( sorted( list( instruments.keys() ) ) ) + '.' )

        elif nadir_satellite not in instruments[nadir_instrument]['valid_satellites']: 

            print( f'Satellite {nadir_satellite} has no instrument {nadir_instrument}, ' + \
                'or at least it is not registered in this package. The instrument ' + \
                f'{nadir_instrument} has been incorporated for the following satellites: ' + \
                ", ".join( instruments[nadir_instrument]['valid_satellites'] ) + "." )

        else: 

            ret = execute_rotation_collocation( missions, timerange, ro_processing_center, 
                    nadir_instrument, nadir_satellite, output_file, nodata=args.nodata )

            if ret['status'] == "fail": 
                print( 'Unsuccessful. {:}: {:}'.format( ret['messages'][-1], ret['comments'][-1] ) )
            else: 
                print( 'Successful completion.' )

    else: 
        print( 'No command provided. Valid commands are "setdefaults" and "execute".' )
        return


if __name__ == "__main__": 
    main()
    pass


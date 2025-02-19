#!/usr/bin/env python3

from ..Utilities.TimeStandards import Time
import requests
import os

def main(): 

    #  Initialize leap second file. 

    now = Time()

    #  Initialize satellite history file. 

    tmpfile = os.getenv( "SATELLITEHISTORY" )
    if tmpfile is None: 
        HOME = os.path.expanduser( "~" )
        tmpfile = os.path.join( HOME, "SATELLIT_I20.SAT" )

    try: 
        r = requests.get( "http://ftp.aiub.unibe.ch/BSWUSER54/CONFIG/SATELLIT_I20.SAT" )
        if r.status_code == 200: 
            f = open( tmpfile, "w" )
            f.write( r.text )
            f.close()
    except:
        pass

    print( tmpfile )


if __name__ == "__main__": 
    main()
    pass


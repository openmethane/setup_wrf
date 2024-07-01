import datetime
import re
import os
import argparse
import math
import f90nml
import shutil
import subprocess
import glob
import pdb
import copy
import stat
import netCDF4
from setup_runs.wrf.fetch_fnl import download_gdas_fnl_data
from setup_runs.wrf.read_config_wrf import load_wrf_config
from setup_runs.utils import compress_nc_file

## get command line arguments
parser = argparse.ArgumentParser()

parser.add_argument(
    "-c",
    "--configFile",
    help="Path to configuration file",
    default="config/wrf/config.nci.json",
)
args = parser.parse_args()
configFile = args.configFile

# load config file and create WRFConfig object
wrf_config = load_wrf_config(configFile)

scripts = {}
dailyScriptNames = ["run", "cleanup"]
script_names = ["main", "run", "cleanup"]
script_paths = [
    wrf_config.main_script_template,
    wrf_config.run_script_template,
    wrf_config.cleanup_script_template,
]
for script_name, script_path in zip(script_names, script_paths):
    ## read the template run script
    assert os.path.exists(script_path), f"No template script was found at {script_path}"
    try:
        f = open(script_path, "rt")
        scripts[script_name] = f.readlines()
        f.close()
    except Exception as e:
        print("Problem reading in template {} script".format(script_name))
        print(str(e))


def decode_bytes(x):
    if isinstance(x, bytes):
        x = x.decode("utf-8")
    return x


def purge(dir, pattern):
    for f in os.listdir(dir):
        if re.search(pattern, f) is not None:
            print("deleting:", pattern, "- file:", f)
            os.remove(os.path.join(dir, f))


def move_pattern_to_dir(sourceDir, pattern, destDir):
    for f in os.listdir(sourceDir):
        if re.search(pattern, f) is not None:
            os.rename(os.path.join(sourceDir, f), os.path.join(destDir, f))


def link_pattern_to_dir(sourceDir, pattern, destDir):
    for f in os.listdir(sourceDir):
        if re.search(pattern, f) is not None:
            src = os.path.join(sourceDir, f)
            dst = os.path.join(destDir, f)
            if not os.path.exists(dst):
                os.symlink(src, dst)


def grep_file(regex, inFile):
    fl = open(inFile, "r")
    lines = fl.readlines()
    fl.close()
    out = [line for line in lines if line.find(regex) >= 0]
    return out


def grep_lines(regex, lines):
    if isinstance(lines, str):
        lines = lines.split("\n")
    out = [line for line in lines if line.find(regex) >= 0]
    return out


def symlink_file(input_directory, output_directory, filename):
    src = os.path.join(input_directory, filename)
    assert os.path.exists(src), "Cannot find script {} ...".format(filename)
    dst = os.path.join(output_directory, filename)
    if not os.path.exists(dst):
        os.symlink(src, dst)


## calculate the number of jobs
run_length_hours = (
    wrf_config.end_date - wrf_config.start_date
).total_seconds() / 3600.0
number_of_jobs = int(math.ceil(run_length_hours / float(wrf_config.num_hours_per_run)))

## check that namelist template files are present
WPSnmlPath = wrf_config.namelist_wps
WRFnmlPath = wrf_config.namelist_wrf
assert os.path.exists(WPSnmlPath), "File WPS namelist not found at {}".format(
    WPSnmlPath
)
assert os.path.exists(WRFnmlPath), "File WRF namelist not found at {}".format(
    WRFnmlPath
)

## read the WPS
WPSnml = f90nml.read(WPSnmlPath)
WRFnml = f90nml.read(WRFnmlPath)

## check that the parameters do agree between the WRF and WPS namelists
## parameters that should agree for the WRF and WPS namelists
namelistParamsThatShouldAgree = [
    {
        "wrf_var": "max_dom",
        "wrf_group": "domains",
        "wps_var": "max_dom",
        "wps_group": "share",
    },
    {
        "wrf_var": "interval_seconds",
        "wrf_group": "time_control",
        "wps_var": "interval_seconds",
        "wps_group": "share",
    },
    {
        "wrf_var": "parent_id",
        "wrf_group": "domains",
        "wps_var": "parent_id",
        "wps_group": "geogrid",
    },
    {
        "wrf_var": "parent_grid_ratio",
        "wrf_group": "domains",
        "wps_var": "parent_grid_ratio",
        "wps_group": "geogrid",
    },
    {
        "wrf_var": "i_parent_start",
        "wrf_group": "domains",
        "wps_var": "i_parent_start",
        "wps_group": "geogrid",
    },
    {
        "wrf_var": "j_parent_start",
        "wrf_group": "domains",
        "wps_var": "j_parent_start",
        "wps_group": "geogrid",
    },
    {
        "wrf_var": "e_we",
        "wrf_group": "domains",
        "wps_var": "e_we",
        "wps_group": "geogrid",
    },
    {
        "wrf_var": "e_sn",
        "wrf_group": "domains",
        "wps_var": "e_sn",
        "wps_group": "geogrid",
    },
    {"wrf_var": "dx", "wrf_group": "domains", "wps_var": "dx", "wps_group": "geogrid"},
    {"wrf_var": "dy", "wrf_group": "domains", "wps_var": "dy", "wps_group": "geogrid"},
]

print("\t\tCheck for consistency between key parameters of the WRF and WPS namelists")
for paramDict in namelistParamsThatShouldAgree:
    WRFval = WRFnml[paramDict["wrf_group"]][paramDict["wrf_var"]]
    WPSval = WPSnml[paramDict["wps_group"]][paramDict["wps_var"]]
    ## the dx,dy variables need special treatment - they are handled differently in the two namelists
    if paramDict["wrf_var"] in ["dx", "dy"]:
        if WPSnml["share"]["max_dom"] == 1:
            if isinstance(WRFval, list):
                assert (
                    WRFval[0] == WPSval
                ), "Mismatched values for variable {} between the WRF and WPS namelists".format(
                    paramDict["wrf_var"]
                )
            else:
                assert (
                    WRFval == WPSval
                ), "Mismatched values for variable {} between the WRF and WPS namelists".format(
                    paramDict["wrf_var"]
                )
        else:
            expectedVal = [float(WPSnml["geogrid"][paramDict["wps_var"]])]
            for idom in range(1, WPSnml["share"]["max_dom"]):
                try:
                    expectedVal.append(
                        expectedVal[-1]
                        / float(WPSnml["geogrid"]["parent_grid_ratio"][idom])
                    )
                except Exception:
                    pdb.set_trace()
            ##
            assert (
                len(WRFval) == len(expectedVal)
            ), "Mismatched length for variable {} between the WRF and WPS namelists".format(
                paramDict["wrf_var"]
            )
            assert all(
                [a == b for a, b in zip(WRFval, expectedVal)]
            ), "Mismatched values for variable {} between the WRF and WPS namelists".format(
                paramDict["wrf_var"]
            )
    else:
        assert type(WRFval) == type(
            WPSval
        ), "Mismatched type for variable {} between the WRF and WPS namelists".format(
            paramDict["wrf_var"]
        )
        if isinstance(WRFval, list):
            assert (
                len(WRFval) == len(WPSval)
            ), "Mismatched length for variable {} between the WRF and WPS namelists".format(
                paramDict["wrf_var"]
            )
            assert all(
                [a == b for a, b in zip(WRFval, WPSval)]
            ), "Mismatched values for variable {} between the WRF and WPS namelists".format(
                paramDict["wrf_var"]
            )
        else:
            assert (
                WRFval == WPSval
            ), "Mismatched values for variable {} between the WRF and WPS namelists".format(
                paramDict["wrf_var"]
            )

## get the number of domains
nDom = WPSnml["share"]["max_dom"]

## get the total run length
run_length_total_hours = wrf_config.num_hours_per_run + wrf_config.num_hours_spin_up

## check that the output directory exists - if not, create it
os.makedirs(wrf_config.run_dir, exist_ok=True)

print("\t\tGenerate the main coordination script")

## write out the main coordination script

############## EDIT: the following are the substitutions used for the main run script
substitutions = {
    "STARTDATE": wrf_config.start_date.strftime("%Y%m%d%H"),
    "njobs": "{}".format(number_of_jobs),
    "nhours": "{}".format(wrf_config.num_hours_per_run),
    "RUNNAME": wrf_config.run_name,
    "NUDGING": "{}".format(not wrf_config.restart).lower(),
    "runAsOneJob": "{}".format(wrf_config.run_as_one_job).lower(),
    "RUN_DIR": wrf_config.run_dir,
}
############## end edit section #####################################################

## do the substitutions
thisScript = copy.copy(scripts["main"])
for avail_key in list(substitutions.keys()):
    key = "${%s}" % avail_key
    value = substitutions[avail_key]
    thisScript = [item.replace(key, value) for item in thisScript]
## write out the lines
scriptFile = "{}.sh".format("main")
scriptPath = os.path.join(wrf_config.run_dir, scriptFile)
f = open(scriptPath, "w")
f.writelines(thisScript)
f.close()
## make executable
os.chmod(scriptPath, os.stat(scriptPath).st_mode | stat.S_IEXEC)

## loop through the different days
for ind_job in range(number_of_jobs):
    job_start = (
        wrf_config.start_date
        + datetime.timedelta(seconds=3600 * ind_job * int(wrf_config.num_hours_per_run))
        - datetime.timedelta(seconds=3600 * int(wrf_config.num_hours_spin_up))
    )

    job_start_usable = wrf_config.start_date + datetime.timedelta(
        seconds=3600 * ind_job * int(wrf_config.num_hours_per_run)
    )

    job_end = wrf_config.start_date + datetime.timedelta(
        seconds=3600 * (ind_job + 1) * int(wrf_config.num_hours_per_run)
    )

    print("Start preparation for the run beginning {}".format(job_start_usable.date()))
    ##
    yyyymmddhh_start = job_start_usable.strftime("%Y%m%d%H")
    run_dir_with_date: str = os.path.join(wrf_config.run_dir, yyyymmddhh_start)

    os.makedirs(run_dir_with_date, exist_ok=True)
    os.chdir(run_dir_with_date)

    ## check that the WRF initialisation files exist
    print("\tCheck that the WRF initialisation files exist")
    wrfbdyPath = os.path.join(run_dir_with_date, "wrfbdy_d01")  ## check for the BCs
    wrfInitFilesExist = os.path.exists(wrfbdyPath)
    for iDom in range(nDom):
        dom = "d0{}".format(iDom + 1)
        wrfinputPath = os.path.join(
            run_dir_with_date, "wrfinput_{}".format(dom)
        )  ## check for the ICs
        wrfInitFilesExist = wrfInitFilesExist and os.path.exists(wrfinputPath)
        wrflowinpPath = os.path.join(
            run_dir_with_date, "wrflowinp_{}".format(dom)
        )  ## check for SSTs
        wrfInitFilesExist = wrfInitFilesExist and os.path.exists(wrflowinpPath)
    ##
    if not wrf_config.only_edit_namelists:
        if not wrfInitFilesExist:
            print("\t\tThe WRF initialisation files did not exist...")
            # Check that the topography files exist
            geoFilesExist = True
            print("\tCheck that the geo_em files exist")
            for iDom in range(nDom):
                dom = "d0{}".format(iDom + 1)
                geoFile = "geo_em.{}.nc".format(dom)
                geoPath = os.path.join(wrf_config.nml_dir, geoFile)
                if not os.path.exists(geoPath):
                    geoFilesExist = False
            ## If not, produce them
            if geoFilesExist:
                print("\t\tThe geo_em files were indeed found")
            else:
                print("\t\tThe geo_em files did not exist - create them")
                ## copy the WPS namelist substituting the geog_data_path
                WPSnml["geogrid"]["geog_data_path"] = wrf_config.geog_data_path
                dst = os.path.join(run_dir_with_date, "namelist.wps")
                WPSnml.write(dst)
                ## copy the geogrid table
                src = wrf_config.geogrid_tbl
                assert os.path.exists(src), "Cannot find GEOGRID.TBL at {} ...".format(
                    src
                )

                geogridFolder = os.path.join(run_dir_with_date, "geogrid")
                os.makedirs(geogridFolder, exist_ok=True)

                ##
                dst = os.path.join(run_dir_with_date, "geogrid", "GEOGRID.TBL")
                if os.path.exists(dst):
                    os.remove(dst)
                os.symlink(src, dst)
                ## link to the geogrid.exe program
                src = wrf_config.geogrid_exe
                assert os.path.exists(src), "Cannot find geogrid.exe at {} ...".format(
                    src
                )
                dst = os.path.join(run_dir_with_date, "geogrid.exe")
                if not os.path.exists(dst):
                    os.symlink(src, dst)
                ## move to the directory and run geogrid.exe
                os.chdir(run_dir_with_date)
                print(
                    "\t\tRun geogrid at {}".format(
                        datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                    )
                )
                p = subprocess.Popen(
                    ["./geogrid.exe"], stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                stdout, stderr = p.communicate()
                stdout = decode_bytes(stdout)
                stderr = decode_bytes(stderr)
                ##
                f = open("geogrid.log.stdout", "w")
                f.writelines(stdout)
                f.close()
                ##
                f = open("geogrid.log.stderr", "w")
                f.writelines(stderr)
                f.close()
                ## check that it ran
                dom = "d0{}".format(nDom)
                geoFile = "geo_em.{}.nc".format(dom)
                assert os.path.exists(
                    geoFile
                ), "./geogrid.exe did not produce expected output..."
                ##
                src = "namelist.wps"
                dst = "namelist.wps.geogrid"
                os.rename(src, dst)
                ## compress the output
                print("\tCompress the geo_em files")
                for iDom in range(nDom):
                    dom = "d0{}".format(iDom + 1)
                    geoFile = "geo_em.{}.nc".format(dom)
                    compress_nc_file(geoFile)
                    ## move the file to the namelist directory
                    src = os.path.join(run_dir_with_date, geoFile)
                    dst = os.path.join(wrf_config.nml_dir, geoFile)
                    shutil.move(src, dst)
            ##
            ## link to the geo files
            for iDom in range(nDom):
                dom = "d0{}".format(iDom + 1)
                geoFile = "geo_em.{}.nc".format(dom)
                ## move the file to the namelist directory
                src = os.path.join(wrf_config.nml_dir, geoFile)
                dst = os.path.join(run_dir_with_date, geoFile)
                if not os.path.exists(dst):
                    os.symlink(src, dst)
            ##
            print("\tCheck that the met_em files exist")
            if not os.path.exists(wrf_config.metem_dir):
                os.makedirs(wrf_config.metem_dir, exist_ok=True)
                metemFilesExist = False
            else:
                metemFilesExist = True
                ## check if the met_em files exist
                for hour in range(0, run_length_total_hours + 1, 6):
                    metem_time = job_start + datetime.timedelta(seconds=hour * 3600)
                    metem_time_str = metem_time.strftime("%Y-%m-%d_%H:%M:%S")
                    for iDom in range(nDom):
                        dom = "d0{}".format(iDom + 1)
                        metem_file = os.path.join(
                            wrf_config.metem_dir,
                            "met_em.{}.{}.nc".format(dom, metem_time_str),
                        )
                        metemFilesExist = metemFilesExist and os.path.exists(metem_file)
            ##
            if not metemFilesExist:
                print("\t\tThe met_em files did not exist - create them")
                ##
                os.chdir(run_dir_with_date)
                ## deal with SSTs first
                ##
                ## copy the link_grib script
                src = wrf_config.linkgrib_script
                assert os.path.exists(
                    src
                ), "Cannot find link_grib.csh at {} ...".format(src)
                dst = os.path.join(run_dir_with_date, "link_grib.csh")
                if os.path.exists(dst):
                    os.remove(dst)
                os.symlink(src, dst)
                ## link the ungrib executabble
                src = wrf_config.ungrib_exe
                assert os.path.exists(src), "Cannot find ungrib.exe at {} ...".format(
                    src
                )
                dst = os.path.join(run_dir_with_date, "ungrib.exe")
                if not os.path.exists(dst):
                    os.symlink(src, dst)

                wpsStrDate = (job_start - datetime.timedelta(days=1)).date()
                wpsEndDate = (job_end + datetime.timedelta(days=1)).date()
                nDaysWps = (wpsEndDate - wpsStrDate).days + 1

                ## should we use ERA-Interim analyses?
                if wrf_config.analysis_source == "ERAI":
                    if wrf_config.use_high_res_sst_data:
                        ## configure the namelist
                        ## EDIT: the following are the substitutions used for the WPS namelist
                        WPSnml["share"]["start_date"] = [
                            job_start.strftime("%Y-%m-%d_00:00:00")
                        ] * nDom
                        WPSnml["share"]["end_date"] = [
                            (job_end.date() + datetime.timedelta(days=1)).strftime(
                                "%Y-%m-%d_%H:%M:%S"
                            )
                        ] * nDom
                        WPSnml["share"]["interval_seconds"] = 6 * 60 * 60  ## 24*60*60
                        WPSnml["ungrib"]["prefix"] = "SST"
                        WPSnml["geogrid"]["geog_data_path"] = wrf_config.geog_data_path
                        ## end edit section #####################################################
                        ## write out the namelist
                        if os.path.exists("namelist.wps"):
                            os.remove("namelist.wps")
                        ##
                        WPSnml.write("namelist.wps")

                        sstDir = "sst_tmp"
                        if not os.path.exists(sstDir):
                            os.makedirs(sstDir, exist_ok=True)
                        ##
                        for iDayWps in range(nDaysWps):
                            wpsDate = wpsStrDate + datetime.timedelta(days=iDayWps)
                            ## check for the monthly file
                            monthlyFile = wpsDate.strftime(
                                wrf_config.sst_monthly_pattern
                            )
                            monthlyFileSrc = os.path.join(
                                wrf_config.sst_monthly_dir, monthlyFile
                            )
                            monthlyFileDst = os.path.join(sstDir, monthlyFile)
                            if os.path.exists(monthlyFileSrc) and (
                                not os.path.exists(monthlyFileDst)
                            ):
                                if not os.path.exists(monthlyFileDst):
                                    os.symlink(monthlyFileSrc, monthlyFileDst)
                            ## check for the daily file
                            dailyFile = wpsDate.strftime(wrf_config.sst_daily_pattern)
                            dailyFileSrc = os.path.join(
                                wrf_config.sst_daily_dir, dailyFile
                            )
                            dailyFileDst = os.path.join(sstDir, dailyFile)
                            if os.path.exists(dailyFileSrc) and (
                                not os.path.exists(dailyFileDst)
                            ):
                                if not os.path.exists(dailyFileDst):
                                    os.symlink(dailyFileSrc, dailyFileDst)
                        ##
                        wpsStrDateStr = wpsStrDate.strftime("%Y-%m-%d_%H:%M:%S")
                        wpsEndDateEnd = wpsEndDate.strftime("%Y-%m-%d_%H:%M:%S")
                        ##
                        purge(run_dir_with_date, "GRIBFILE*")
                        print(
                            "\t\tRun link_grib for the SST data at {}".format(
                                datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                            )
                        )
                        p = subprocess.Popen(
                            ["./link_grib.csh", os.path.join(sstDir, "*")],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                        )
                        stdout, stderr = p.communicate()
                        stdout = decode_bytes(stdout)
                        stderr = decode_bytes(stderr)
                        ##
                        f = open("link_grib_sst.log.stdout", "w")
                        f.writelines(stdout)
                        f.close()
                        ##
                        f = open("link_grib_sst.log.stderr", "w")
                        f.writelines(stderr)
                        f.close()
                        ## check that it ran
                        ## time.sleep(0.2)
                        gribmatches = [
                            f
                            for f in os.listdir(run_dir_with_date)
                            if re.search("GRIBFILE", f) is not None
                        ]
                        if len(gribmatches) == 0:
                            raise RuntimeError("Gribfiles not linked successfully...")
                        ## link to the SST Vtable
                        src = wrf_config.sst_vtable
                        assert os.path.exists(src), "SST Vtable expected at {}".format(
                            src
                        )
                        dst = "Vtable"
                        if os.path.exists(dst):
                            os.remove(dst)
                        os.symlink(src, dst)
                        purge(run_dir_with_date, "SST:*")
                        purge(run_dir_with_date, "PFILE:*")
                        ## run ungrib on the SST files
                        print(
                            "\t\tRun ungrib for the SST data at {}".format(
                                datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                            )
                        )
                        p = subprocess.Popen(
                            ["./ungrib.exe"],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                        )
                        stdout, stderr = p.communicate()
                        stdout = decode_bytes(stdout)
                        stderr = decode_bytes(stderr)
                        ##
                        f = open("ungrib_sst.log.stdout", "w")
                        f.writelines(stdout)
                        f.close()
                        ##
                        f = open("ungrib_sst.log.stderr", "w")
                        f.writelines(stderr)
                        f.close()

                        ## check that it ran
                        ## matches = grep_file('Successful completion of ungrib', logfile)
                        matches = grep_lines("Successful completion of ungrib", stdout)
                        if len(matches) == 0:
                            raise RuntimeError(
                                "Success message not found in ungrib logfile..."
                            )

                        src = "namelist.wps"
                        dst = "namelist.wps.sst"
                        os.rename(src, dst)

                    analysisDir = "analysis_tmp"
                    if not os.path.exists(analysisDir):
                        os.makedirs(analysisDir, exist_ok=True)

                    for pattern in [
                        wrf_config.analysis_pattern_surface,
                        wrf_config.analysis_pattern_upper,
                    ]:
                        files = set([])
                        for iDayWps in range(nDaysWps):
                            wpsDate = wpsStrDate + datetime.timedelta(days=iDayWps)
                            patternWithDates = wpsDate.strftime(pattern)
                            files = files.union(set(glob.glob(patternWithDates)))
                        ##
                        files = list(files)
                        files.sort()
                        if pattern == "analysis_pattern_upper":
                            ## for the upper-level files, be selective and use only those that contain the relevant range of dates
                            for ifile, filename in enumerate(files):
                                basepieces = os.path.basename(filename).split("_")
                                fileStartDateStr = os.path.basename(filename).split(
                                    "_"
                                )[-2]
                                fileEndDateStr = os.path.basename(filename).split("_")[
                                    -1
                                ]
                                fileStartDate = datetime.datetime.strptime(
                                    fileStartDateStr, "%Y%m%d"
                                ).date()
                                fileEndDate = datetime.datetime.strptime(
                                    fileEndDateStr, "%Y%m%d"
                                ).date()
                                ##
                                if (
                                    fileStartDate <= wpsStrDate
                                    and wpsStrDate <= fileEndDate
                                ):
                                    ifileStart = ifile
                                ##
                                if (
                                    fileStartDate <= wpsEndDate
                                    and wpsEndDate <= fileEndDate
                                ):
                                    ifileEnd = ifile
                        else:
                            ## for the surface files use all those that match
                            ifileStart = 0
                            ifileEnd = len(files) - 1
                        ##
                        for ifile in range(ifileStart, ifileEnd + 1):
                            src = files[ifile]
                            dst = os.path.join(analysisDir, os.path.basename(src))
                            if not os.path.exists(dst):
                                os.symlink(src, dst)

                        ## prepare to run link_grib.csh
                        linkGribCmds = [
                            "./link_grib.csh",
                            os.path.join(analysisDir, "*"),
                        ]

                else:
                    ## consider the case that we are using the FNL datax
                    nIntervals = (
                        int(round((job_end - job_start).total_seconds() / 3600.0 / 6.0))
                        + 1
                    )
                    FNLtimes = [
                        job_start + datetime.timedelta(hours=6 * hi)
                        for hi in range(nIntervals)
                    ]
                    FNLfiles = [
                        time.strftime("gdas1.fnl0p25.%Y%m%d%H.f00.grib2")
                        for time in FNLtimes
                    ]
                    ## if the FNL data exists, don't bother downloading
                    allFNLfilesExist = all(
                        [os.path.exists(FNLfile) for FNLfile in FNLfiles]
                    )
                    if allFNLfilesExist:
                        print(
                            "\t\tAll FNL files were found - do not repeat the download"
                        )
                    else:
                        ## otherwise get it all
                        FNLfiles = download_gdas_fnl_data(
                            orcid=wrf_config.orcid,
                            api_token=wrf_config.rda_ucar_edu_api_token,
                            target_dir=run_dir_with_date,
                            download_dts=FNLtimes,
                        )
                    linkGribCmds = ["./link_grib.csh"] + FNLfiles
                    ## optionally take a regional subset
                    if wrf_config.regional_subset_of_grib_data:
                        geoFile = "geo_em.d01.nc"
                        ## find the geographical region, and add a few degrees on either side
                        geoStrs = {}
                        nc = netCDF4.Dataset(geoFile)
                        for varname in ["XLAT_M", "XLONG_M"]:
                            coords = nc.variables[varname][:]
                            coords = [coords.min(), coords.max()]
                            coords = [
                                math.floor((coords[0]) / 5.0 - 1) * 5,
                                math.ceil((coords[1]) / 5.0 + 1) * 5,
                            ]
                            coordStr = "{}:{}".format(coords[0], coords[1])
                            geoStrs[varname] = coordStr
                        nc.close()
                        ## use wgrib2 that
                        for FNLfile in FNLfiles:
                            tmpfile = os.path.join("/tmp", os.path.basename(FNLfile))
                            print("\t\tSubset the grib file", os.path.basename(FNLfile))
                            stdout, stderr = subprocess.Popen(
                                [
                                    "wgrib2",
                                    FNLfile,
                                    "-small_grib",
                                    geoStrs["XLONG_M"],
                                    geoStrs["XLAT_M"],
                                    tmpfile,
                                ],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                            ).communicate()
                            if len(stderr) > 0:
                                print(stderr)
                                raise RuntimeError(
                                    "Errors found when running wgrib2..."
                                )
                            ## use the subset instead - delete the original and put the subset in its place
                            os.remove(FNLfile)
                            shutil.copyfile(tmpfile, FNLfile)

                ## EDIT: the following are the substitutions used for the WPS namelist
                WPSnml["share"]["start_date"] = [
                    job_start.strftime("%Y-%m-%d_%H:%M:%S")
                ] * nDom
                WPSnml["share"]["end_date"] = [
                    job_end.strftime("%Y-%m-%d_%H:%M:%S")
                ] * nDom
                WPSnml["ungrib"]["prefix"] = "ERA"
                WPSnml["share"]["interval_seconds"] = 6 * 60 * 60
                ## end edit section #####################################################

                ## write out the namelist
                if os.path.exists("namelist.wps"):
                    os.remove("namelist.wps")
                WPSnml.write("namelist.wps")
                ##
                purge(run_dir_with_date, "GRIBFILE*")
                print(
                    "\t\tRun link_grib for the FNL data at {}".format(
                        datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                    )
                )
                p = subprocess.Popen(
                    linkGribCmds, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                stdout, stderr = p.communicate()
                stdout = decode_bytes(stdout)
                stderr = decode_bytes(stderr)
                ##
                f = open("link_grib_fnl.log.stdout", "w")
                f.writelines(stdout)
                f.close()
                ##
                f = open("link_grib_fnl.log.stderr", "w")
                f.writelines(stderr)
                f.close()

                ## check that it ran
                gribmatches = [
                    f
                    for f in os.listdir(run_dir_with_date)
                    if re.search("GRIBFILE", f) is not None
                ]
                if len(gribmatches) == 0:
                    raise RuntimeError("Gribfiles not linked successfully...")

                ###################
                # Run ungrib
                ###################

                ## link to the relevant Vtable
                src = wrf_config.analysis_vtable
                assert os.path.exists(src), "Analysis Vtable expected at {}".format(src)
                dst = os.path.join(run_dir_with_date, "Vtable")
                if os.path.exists(dst):
                    os.remove(dst)
                os.symlink(src, dst)

                purge(run_dir_with_date, "ERA:*")
                ## with open('ungrib.log.era', 'w') as output_f:
                print(
                    "\t\tRun ungrib for the ERA data at {}".format(
                        datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                    )
                )
                p = subprocess.Popen(
                    ["./ungrib.exe"], stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                stdout, stderr = p.communicate()
                stdout = decode_bytes(stdout)
                stderr = decode_bytes(stderr)
                ##
                f = open("ungrib_era.log.stdout", "w")
                f.writelines(stdout)
                f.close()
                ##
                f = open("ungrib_era.log.stderr", "w")
                f.writelines(stderr)
                f.close()

                ## FIXME: check that it worked
                matches = grep_lines("Successful completion of ungrib", stdout)
                if len(matches) == 0:
                    print(stdout)
                    raise RuntimeError("Success message not found in ungrib logfile...")

                ## if we are using the FNL analyses, delete the downloaded FNL files
                if wrf_config.analysis_source == "FNL":
                    for FNLfile in FNLfiles:
                        os.remove(FNLfile)

                #############
                # Run metgrid
                #############
                print(
                    "\t\tRun metgrid at {}".format(
                        datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                    )
                )
                metgriddir = os.path.join(run_dir_with_date, "metgrid")
                os.makedirs(metgriddir, exist_ok=True)

                WPSnml["metgrid"]["fg_name"] = ["ERA"]
                if wrf_config.use_high_res_sst_data:
                    WPSnml["metgrid"]["fg_name"].append("SST")
                ##
                ## link to the relevant METGRID.TBL
                src = wrf_config.metgrid_tbl
                assert os.path.exists(src), "Cannot find METGRID.TBL at {} ...".format(
                    src
                )
                dst = os.path.join(metgriddir, "METGRID.TBL")
                if not os.path.exists(dst):
                    os.symlink(src, dst)
                ## link to metgrid.exe
                src = wrf_config.metgrid_exe
                assert os.path.exists(src), "Cannot find metgrid.exe at {} ...".format(
                    src
                )
                dst = os.path.join(run_dir_with_date, "metgrid.exe")
                if not os.path.exists(dst):
                    os.symlink(src, dst)
                ##
                ## logfile = 'metgrid_stderr_stdout.log'
                ## with open(logfile, 'w') as output_f:
                p = subprocess.Popen(
                    ["./metgrid.exe"], stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                stdout, stderr = p.communicate()
                stdout = decode_bytes(stdout)
                stderr = decode_bytes(stderr)
                ##
                f = open("metgrid.log.stdout", "w")
                f.writelines(stdout)
                f.close()
                ##
                f = open("metgrid.log.stderr", "w")
                f.writelines(stderr)
                f.close()

                matches = grep_lines("Successful completion of metgrid", stdout)
                if len(matches) == 0:
                    raise RuntimeError(
                        "Success message not found in metgrid logfile..."
                    )

                purge(run_dir_with_date, "ERA:*")
                if wrf_config.use_high_res_sst_data:
                    purge(run_dir_with_date, "SST:*")
                purge(run_dir_with_date, "FILE:*")
                purge(run_dir_with_date, "PFILE:*")
                purge(run_dir_with_date, "GRIB:*")
                purge(run_dir_with_date, "fort.*")

                ## move the met_em files into the combined METEM_DIR directory
                move_pattern_to_dir(
                    sourceDir=run_dir_with_date,
                    pattern="met_em*",
                    destDir=wrf_config.metem_dir,
                )

            ## link to the met_em files
            os.chdir(run_dir_with_date)
            print("\t\tlink to the met_em files")
            for hour in range(0, run_length_total_hours + 1, 6):
                metem_time = job_start + datetime.timedelta(seconds=hour * 3600)
                metem_time_str = metem_time.strftime("%Y-%m-%d_%H:%M:%S")
                for iDom in range(nDom):
                    dom = "d0{}".format(iDom + 1)
                    metem_file = "met_em.{}.{}.nc".format(dom, metem_time_str)
                    src = os.path.join(wrf_config.metem_dir, metem_file)
                    assert os.path.exists(
                        src
                    ), "Cannot find met_em file at {} ...".format(src)
                    dst = os.path.join(run_dir_with_date, metem_file)
                    if not os.path.exists(dst):
                        os.symlink(src, dst)

    if (not wrf_config.only_edit_namelists) and (not wrfInitFilesExist):
        ## find a met_em file and read the number of atmospheric and soil levels
        metempattern = os.path.join(wrf_config.metem_dir, "met_em.d*.nc")
        ##
        metemfiles = glob.glob(metempattern)
        assert len(metemfiles) > 0, "No met_em files found..."
        metemfile = metemfiles[0]
        nc = netCDF4.Dataset(metemfile)
        nz_metem = len(nc.dimensions["num_metgrid_levels"])
        nz_soil = len(nc.dimensions["num_st_layers"])
        nc.close()
    else:
        if wrf_config.analysis_source == "ERAI":
            nz_metem = 38
            nz_soil = 4
        elif wrf_config.analysis_source == "FNL":
            nz_metem = 27
            nz_soil = 4

    ## configure the WRF namelist
    print("\t\tconfigure the WRF namelist")
    ########## EDIT: the following are the substitutions used for the WRF namelist
    WRFnml["time_control"]["start_year"] = [job_start.year] * nDom
    WRFnml["time_control"]["start_month"] = [job_start.month] * nDom
    WRFnml["time_control"]["start_day"] = [job_start.day] * nDom
    WRFnml["time_control"]["start_hour"] = [job_start.hour] * nDom
    WRFnml["time_control"]["start_minute"] = [job_start.minute] * nDom
    WRFnml["time_control"]["start_second"] = [job_start.second] * nDom
    ##
    WRFnml["time_control"]["end_year"] = [job_end.year] * nDom
    WRFnml["time_control"]["end_month"] = [job_end.month] * nDom
    WRFnml["time_control"]["end_day"] = [job_end.day] * nDom
    WRFnml["time_control"]["end_hour"] = [job_end.hour] * nDom
    WRFnml["time_control"]["end_minute"] = [job_end.minute] * nDom
    WRFnml["time_control"]["end_second"] = [job_end.second] * nDom
    ########## end edit section #####################################################
    ##
    WRFnml["time_control"]["restart"] = wrf_config.restart
    ##
    WRFnml["domains"]["num_metgrid_levels"] = nz_metem
    WRFnml["domains"]["num_metgrid_soil_levels"] = nz_soil
    ##
    nmlfile = "namelist.input"
    if os.path.exists(nmlfile):
        os.remove(nmlfile)
    WRFnml.write(nmlfile)
    ##
    # Get real.exe and WRF.exe
    src = wrf_config.real_exe
    assert os.path.exists(src), "Cannot find real.exe at {} ...".format(src)
    dst = os.path.join(run_dir_with_date, "real.exe")
    if os.path.exists(dst):
        os.remove(dst)
    os.symlink(src, dst)
    ##
    src = wrf_config.wrf_exe
    assert os.path.exists(src), "Cannot find wrf.exe at {} ...".format(src)
    dst = os.path.join(run_dir_with_date, "wrf.exe")
    if os.path.exists(dst):
        os.remove(dst)
    os.symlink(src, dst)

    # get background checking script to initiate averaging
    src = wrf_config.check_wrfout_in_background_script
    assert os.path.exists(src), "Cannot find wrfout checking  script at {} ...".format(
        src
    )
    dst = os.path.join(run_dir_with_date, "checkWrfoutInBackground.py")
    if os.path.exists(dst):
        os.remove(dst)
    os.symlink(src, dst)

    # Get tables
    link_pattern_to_dir(
        sourceDir=wrf_config.wrf_run_dir,
        pattern=wrf_config.wrf_run_tables_pattern,
        destDir=run_dir_with_date,
    )

    # link to scripts from the namelist and target directories
    for input_directory, scripts_to_copy in (
        (wrf_config.target_dir, wrf_config.scripts_to_copy_from_target_dir),
        (wrf_config.nml_dir, wrf_config.scripts_to_copy_from_nml_dir),
    ):
        scripts_to_copy = scripts_to_copy.split(",")

        for script_to_copy in scripts_to_copy:
            symlink_file(input_directory, run_dir_with_date, script_to_copy)

    if (not wrf_config.only_edit_namelists) and (not wrfInitFilesExist):
        ##
        logfile = "real_stderr_stdout.log"

        print(
            "\t\tRun real.exe at {}".format(
                datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            )
        )
        p = subprocess.Popen(
            ["mpirun", "-np", "1", "./real.exe"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = p.communicate()
        stdout = decode_bytes(stdout)
        stderr = decode_bytes(stderr)
        ##
        f = open("real.log.stdout", "w")
        f.writelines(stdout)
        f.close()
        ##
        f = open("real.log.stderr", "w")
        f.writelines(stderr)
        f.close()

        rsloutfile = "rsl.out.0000"
        matches = grep_file("SUCCESS COMPLETE REAL_EM INIT", rsloutfile)
        if len(matches) == 0:
            raise RuntimeError(
                "Success message not found in real.exe logfile (rsl.out.0000)..."
            )
        ##
        if os.path.exists("link_grib.csh"):
            os.remove("link_grib.csh")
        if os.path.exists("Vtable"):
            os.remove("Vtable")
        if os.path.exists("metgrid"):
            shutil.rmtree("metgrid")
        if os.path.exists("metgrid.exe"):
            os.remove("metgrid.exe")
        if os.path.exists("ungrib.exe"):
            os.remove("ungrib.exe")

        ## optionally delete the met_em files once they have been used
        if wrf_config.delete_metem_files:
            purge(wrf_config.metem_dir, "met_em*")

    ## clean up the links to the met_em files regardless, as they are no longer needed
    purge(run_dir_with_date, "met_em*")

    ## generate the run and cleanup scripts
    print("\t\tGenerate the run and cleanup script")

    ########## EDIT: the following are the substitutions used for the per-run cleanup and run scripts
    substitutions = {
        "RUN_DIR": run_dir_with_date,
        "RUNSHORT": wrf_config.run_name[:8],
        "STARTDATE": job_start_usable.strftime("%Y%m%d"),
        "firstTimeToKeep": job_start_usable.strftime("%Y-%m-%dT%H%M"),
    }
    ########## end edit section #####################################################

    ## write out the run and cleanup script
    for dailyScriptName in dailyScriptNames:
        ## do the substitutions
        thisScript = copy.copy(scripts[dailyScriptName])
        for avail_key in list(substitutions.keys()):
            key = "${%s}" % avail_key
            value = substitutions[avail_key]
            thisScript = [item.replace(key, value) for item in thisScript]
        ## write out the lines
        scriptFile = "{}.sh".format(dailyScriptName)
        scriptPath = os.path.join(run_dir_with_date, scriptFile)
        f = open(scriptPath, "w")
        f.writelines(thisScript)
        f.close()
        ## make executable
        os.chmod(scriptPath, os.stat(scriptPath).st_mode | stat.S_IEXEC)

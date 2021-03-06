#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import print_function
import os, shutil
from collections import defaultdict
import numpy as np
from crank.DihedralScanner import DihedralScanner, get_geo_key
from crank.QMEngine import QMEngine
from crank.PriorityQueue import PriorityQueue
from geometric.molecule import Molecule

# extend the DihedralScanner to allow repeating the previous scan process
def repeat_scan_process(self):
    self.push_initial_opt_tasks()
    if len(self.opt_queue) == 0:
        print("No tasks in opt_queue! Exiting..")
        return
    # make sure we're in the rootpath
    os.chdir(self.rootpath)
    self.refined_grid_ids = set()
    self.running_job_path_info = dict()
    self.current_finished_job_results = PriorityQueue()
    # start the iteration from beginning
    while True:
        # print current status
        if self.verbose:
            if len(self.dihedrals) == 2:
                print(self.draw_ramachandran_plot())
            else:
                print(self.draw_ascii_image())
        # this function will try to read cache and decide if new jobs needs to run
        self.launch_opt_jobs()
        # Break if any job was not found in the current cache
        if len(self.running_job_path_info) > 0:  break
        # If all jobs found in the current iteration, parse the results
        current_best_grid_m = dict()
        while len(self.current_finished_job_results) > 0:
            m, grid_id = self.current_finished_job_results.pop()
            if grid_id not in current_best_grid_m or m.qm_energies[0] < current_best_grid_m[grid_id].qm_energies[0]:
                current_best_grid_m[grid_id] = m
        # we only want refined results in current iteration to show in draw_ramachandran_plot()
        self.refined_grid_ids = set()
        # compare the best results between current iteration and all previous iterations
        newly_updated_grid_m = []
        for grid_id, m in current_best_grid_m.items():
            if grid_id not in self.grid_energies:
                if self.verbose:
                    print("First energy for grid_id %s = %f" % (str(grid_id), m.qm_energies[0]))
                self.grid_energies[grid_id] = m.qm_energies[0]
                self.grid_final_geometries[grid_id] = m.xyzs[0]
                newly_updated_grid_m.append((grid_id, m))
            elif m.qm_energies[0] < self.grid_energies[grid_id] - self.energy_decrease_thresh:
                if self.verbose:
                    print("Energy for grid_id %s decreased from %f to %f" % (str(grid_id), self.grid_energies[grid_id], m.qm_energies[0]))
                self.grid_energies[grid_id] = m.qm_energies[0]
                self.grid_final_geometries[grid_id] = m.xyzs[0]
                newly_updated_grid_m.append((grid_id, m))
                # we record the refined_grid_ids here to be printed as green tiles in draw_ramachandran_plot()
                self.refined_grid_ids.add(grid_id)
        # create new tasks for each newly_updated_grid_m
        for grid_id, m in newly_updated_grid_m:
            # every neighbor grid point will get one new task
            for neighbor_gid in self.grid_neighbors(grid_id):
                task = m, grid_id, neighbor_gid
                # all jobs are pushed with the same priority for now, can be adjusted here
                self.opt_queue.push(task)
        # check if all jobs finished
        if len(self.opt_queue) == 0 and len(self.running_job_path_info) == 0:
            print("All optimizations converged at lowest energy. Job Finished!")
            break

DihedralScanner.repeat_scan_process = repeat_scan_process

def rebuild_task_cache(grid_status, scanner):
    """
    Take a dictionary of finished optimizations, rebuild task_cache dictionary
    This function mimics the DihedralScanner.restore_task_cache()

    Parameters:
    ------------
    grid_status = dict(), key is the grid_id, value is a list of job_info. Each job_info is a tuple of (start_geo, end_geo, end_energy).
        * Note: The order of the job_info is important when reproducing the same scan procedure.
    scanner: a DihedralScanner object that has been initialized with dihedrals and grid_spacing attributes

    Returns: None
    ------------
    Upon finish, the new folder 'opt_tmp' will be created, with many empty folders corrsponding to the finished jobs.
    scanner.task_cache will be populated with correct information for repreducing the entire scan process.
    """
    # make sure we're in the root path of scanner
    os.chdir(scanner.rootpath)
    # remove current opt_tmp if exist
    opt_tmp = scanner.tmp_folder_name
    if os.path.isdir(opt_tmp):
        shutil.rmtree(opt_tmp)
    # create a new opt_tmp folder structure
    scanner.create_tmp_folder()
    # rebuild the cache
    for grid_id, job_info_list in grid_status.items():
        tname = 'gid_' + '_'.join('%+04d' % gid for gid in grid_id)
        tmp_folder_path = os.path.join(scanner.tmp_folder_name, tname)
        n_finished_jobs = len(job_info_list)
        for i_job, job_info in enumerate(job_info_list):
            job_path = os.path.join(tmp_folder_path, str(i_job+1))
            os.mkdir(job_path) # empty folder created to mimic the restart behavior
            (start_geo, end_geo, end_energy) = job_info
            job_geo_key = get_geo_key(start_geo)
            scanner.task_cache[grid_id][job_geo_key] = (end_geo, end_energy, job_path)

def get_next_jobs(current_state, verbose=False):
    """
    Take current scan state and generate the next set of optimizations.
    This function will create a new DihedralScanner object and read all information from current_state,
    then reproduce the entire scan from the beginning, finish all cached ones, until a new job is not found in the cache.
    Return a list of new jobs that needs to be finished for the current iteration

    Input:
    -------
    current_state: dict, e.g. {
            'dihedrals': [[0,1,2,3], [1,2,3,4]] ,
            'grid_spacing': 30,
            'elements': ['H', 'C', 'O', ...]
            'init_coords': [geo1, geo2, ..]
            'grid_status': {(30, 60): [(start_geo, end_geo, end_energy), ..], ...}
        }


    Output:
    -------
    next_jobs: dict(), key is the target grid_id, value is a list of new_job. Each new_job is represented by its start_geo
        * Note: the order of new_job should correspond to the finished job_info.
    ]
    """
    dihedrals = current_state['dihedrals']
    grid_spacing = current_state['grid_spacing']
    # rebuild the init_coords_M molecule object
    init_coords_M = Molecule()
    init_coords_M.elem = current_state['elements']
    init_coords_M.xyzs = map(np.array, current_state['init_coords'])
    init_coords_M.build_topology()
    # create a new scanner object
    scanner = DihedralScanner(QMEngine(), dihedrals, grid_spacing, init_coords_M, verbose)
    # rebuild the task_cache for scanner
    rebuild_task_cache(current_state['grid_status'], scanner)
    # run the scanner until some calculation is not found in cache
    scanner.repeat_scan_process()
    # save the new jobs from scanner
    next_jobs = defaultdict(list)
    # we define the order of running jobs based on the path
    job_paths = sorted(scanner.running_job_path_info.keys())
    for job_path in job_paths:
        m, from_grid_id, to_grid_id = scanner.running_job_path_info[job_path]
        next_jobs[to_grid_id].append(m.xyzs[0])
    return next_jobs



def main():
    import argparse, sys, pickle
    parser = argparse.ArgumentParser(description="Take a scan state and return the next set of optimizations", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('statefile', help='File contains the current state')
    #parser.add_argument('-t', '--filetype', choices=['pickle', 'json'], default='pickle', help='File type for statefile')
    parser.add_argument('-v', '--verbose', action='store_true', default=False, help='Print more information while running.')
    args = parser.parse_args()

    # print input command for reproducibility
    print(' '.join(sys.argv))

    # json doesn't work yet because it can not have tuple like (30, 60) as key
    # with open(args.statefile, 'rb') as infile:
    #     if args.filetype == 'pickle':
    #         import pickle
    #         current_state = pickle.load(infile)
    #     elif args.filetype == 'json':
    #         import json
    #         current_state = json.load(infile)

    with open(args.statefile, 'rb') as infile:
        current_state = pickle.load(infile)

    next_jobs = get_next_jobs(current_state, verbose=args.verbose)
    if len(next_jobs) > 0:
        print("Number of jobs to run next for each grid id")
        for grid_id in next_jobs.keys():
            print("%-20s %10d" % (str(grid_id), len(next_jobs[grid_id])))
        with open('next_jobs.pickle', 'wb') as outfile:
            pickle.dump(next_jobs, outfile)
        print("Information for next set of jobs is dumped to next_jobs.pickle")
    else:
        print("Scan has finished. No further job needs to be done")

if __name__ == "__main__":
    main()
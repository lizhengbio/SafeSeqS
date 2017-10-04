import argparse
from argparse import Namespace
import gzip
import io
import itertools
import time
import json
import sys, traceback, os
import multiprocessing
import platform
import logging

import unique

#safeseqs_controller - This process runs the analytical steps of the SAFESEQS pipeline.

class StopStep(Exception):
    pass

#globals
args = None
logfile = ''  #string holding the logfile directory and name
checkpoints = {} #dictionary holding checkpoints in memory
fh_limit = 0 #normal Windows limit is 512, Linux is 1024
barcodemap_list = [] #full list; may include one entry for all "bad" and/or "merge" barcodes if user has requested that they be saved
barcodes_used = []
barcodes_not_used = []
total_reads = 0    
reads_with_bc_not_used = []
reads_with_bc_not_found = []
barcode_files = {} #dict of barcode files open with file handles for the split process. 

    
#Read the command line arguments and return them in args
def get_args():
    global args
    parser = argparse.ArgumentParser()
    parser.add_argument('-d', '--directory', help='Directory Containing Study Fastq Files.', required=True)
    parser.add_argument('-r', '--runname', help='Run Directory will be created in directory with study files.', required=True)
    parser.add_argument('-sf', '--settings', help='Settings file with runtime parameters. File must exist in the data directory', required=True)
    parser.add_argument('-w', '--workers', help='Number of concurrent worker processes to run', type=int, default=1, required=False)
    parser.add_argument('-s','--stepname', help='Start Step to begin Processing', required=False)
    parser.add_argument('-e','--endstep', help='Step Before which to stop Processing', required=False)
    
    
    args = parser.parse_args()
    if args.stepname is not None:
        args.stepname = args.stepname.lower()
    if args.endstep is not None:
        args.endstep = args.endstep.lower()
    return     

 
#The controller needs a file that provides processing parameters including specification
#of data files.  The file must be in the Study Data directory and must be named 'safeseqs.json'.
#Format of the file is:
#
# {"reads_pattern" :   "",
#  "barcodes_pattern" : "",
#  "reads_files" : ["fastq file 1 (just file name - must be in Study Data Directory", "fastq file 2", ... ],
#  "barcodes_files" : ["barcode file 1", "barcode file 2", ...],
#  "barcodemap" : "well barcode association file"}

def getSAFESEQSParams():
    missing_parms = False
    
    safeseqs_file = os.path.join(args.directory, 'safeseqs.json') 
    with open(safeseqs_file) as json_file:    
        parms = json.load(json_file)

    if 'reads_pattern' not in parms:
        print 'Missing reads_pattern from JSON file'
        missing_parms =True

    if 'barcodes_pattern' not in parms:
        print 'Missing barcodes_pattern from JSON file'
        missing_parms =True

    if 'reads_files' not in parms:
        print 'Missing reads_files from JSON file'
        missing_parms =True

    if 'barcodes_files' not in parms:
        print 'Missing barcodes_files from JSON file'
        missing_parms =True
         
    if 'barcodemap' not in parms:
        print 'Missing barcodemap from JSON file'
        missing_parms =True
         
    settings_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.path.pardir, 'data', args.settings) 
    with open(settings_file) as json_file:    
        settings = json.load(json_file)

    parms.update(settings)
    
    if 'uidLength' not in parms:
        print 'Missing uidLength from Settings file'
        missing_parms =True
            
    if 'load_not_used_bc' not in settings:
        parms['load_not_used_bc'] = False
    else:
        parms['load_not_used_bc'] = parms['load_not_used_bc'].lower()
        if parms['load_not_used_bc'] == "yes" or parms['load_not_used_bc'] == "y":
            parms['load_not_used_bc'] = True
        else:
            parms['load_not_used_bc'] = False
        
    if 'save_merge' not in parms:
        parms['save_merge'] = False
    else:
        parms['save_merge'] = parms['save_merge'].lower()
        if parms['save_merge'] == "yes" or parms['save_merge'] == "y":
            parms['save_merge'] = True
        else:
            parms['save_merge'] = False
          
    if missing_parms:
        raise Exception
        
    return parms

 
#set the maximum file handle limit possible on the OS. 
def set_fh_limit():
    global fh_limit
    platform_name = platform.system()
    

    if platform_name.startswith("Windows"):
        import win32file
        fh_limit = win32file._getmaxstdio()
        win32file._setmaxstdio(2048)
        fh_limit = win32file._getmaxstdio()
    elif platform_name.startswith("Linux"):
        import resource
        fh_limit, fh_max = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (2048, fh_max))
        fh_limit, fh_max = resource.getrlimit(resource.RLIMIT_NOFILE)
        
    #Return a smaller number for the controller to work with to allow for additional open file handles.        
    fh_limit = fh_limit-20
        
    return (fh_limit)


#Write the step and data set name to the checkpoint file
def record_checkpoint(step, dataset):
    global checkpoints
    key = step + " " + dataset
    filename = os.path.join(args.directory, args.runname,"checkpoint.txt")
    statusfile = open(filename, 'a')
    statusfile.write(key + "\n")
    statusfile.close()
    #if step and data is not in the dictionary, put it there
    if key not in checkpoints:
        checkpoints[key] = True

    
#Determine whether the Step needs to be run for this set of Data.
def skip_step(step):
    #If the EndStep argument was passed from the command line, the user is controlling the stopping point of processing.
    if args.endstep is not None:
        if step == args.endstep:
            logging.info('Stopping before %s at user request.' %(step))
            print('Stopping before ' + step + ' at user request.')
            #stop all processing
            raise StopStep
            return True

    #If the Step argument was passed from the command line, the user is controlling the start of processing.
    #If this step is equal to or after the requested step staring point, process the combination.
    if args.stepname is not None:
        if step == args.stepname:
            checkpoints['step_found'] = True
            return False
        elif 'step_found' in checkpoints:
            return False
        else:
            logging.info(' Skipping %s at user request.' % step)
            print(' Skipping ' + step + ' at user request.')
            return True
    #If the Start Step argument was NOT passed from the command line, do the step normally.
    else:
        return False        
        
    
#Check if there is a checkpoints file and if this step has been completed on the dataset
def is_done(step, dataset):
    global checkpoints
    key = step + " " + dataset
    filename = os.path.join(args.directory, args.runname,"checkpoint.txt")
    # if the checkpoints dictionary has not yet been loaded, do that first
    if not checkpoints:
        # make sure there is a checkpoints file, then load it to the dictionary
        if os.path.isfile(filename):
            statusfile = open(filename, 'r')
            checkpoint_entry = statusfile.readline().rstrip("\n")
            while checkpoint_entry >"":
                #watch for duplicates entries in the checkpoint file before loading checkpoint dictionary
                if checkpoint_entry not in checkpoints:
                    checkpoints[checkpoint_entry] =True
                #read next line from checkpoint file
                checkpoint_entry = statusfile.readline().rstrip("\n")
            #be sure to close the file after loading    
            statusfile.close()
                
    if key in checkpoints:
        logging.info('   Found %s %s in checkpoints dictionary' %(step, dataset))
        print('   Found ' + step +' ' + dataset + ' in checkpoints dictionary')
        return True
    else:
        return False


#Split the merged input files into one file per barcode with all of the reads for a given 
#barcode in a file.
def split_inputs(parms):
    logging.debug('split_inputs')
    global barcode_files
    global total_reads
    global reads_with_bc_not_found
    global reads_with_bc_not_used
    
    #load the barcode maplist into memory. this list will be used by split and later steps
    load_barcodes(parms)
    
    if skip_step('split') or is_done('split', 'all'):
        return

    logging.info('Split input by barcode Started.')
    print('Split input by barcode Started.')

    #create a subdirectory under the runname directory to store the split files    
    split_directory = os.path.join(parms['resultsDir'], "split")
    if not os.path.isdir(split_directory):
        os.makedirs(split_directory)
    
    start = 0
    first_pass = True
    
    while start <= len(barcodemap_list):
        open_barcode_files(parms, start)
        #Loop through the merged input files, split them by barcode
        loop_pairs(parms, first_pass)       
        #close the files
        for each_file in barcode_files:
            barcode_files[each_file].close()
            
        #clear the dictionary of open barcode files
        barcode_files.clear()
            
        start = start + fh_limit
        first_pass = False

    write_split_stats(parms)
    record_checkpoint('split', 'all' )

    logging.info('Split input by barcode Finished.')
    print('Split input by barcode Finished.')

    
#Load the barcodes from the BarcodeMap.txt into two lists based on whether they have a sample name - indicating whether they are being 'Used' or listed as 'Not Used'. 
#Also, load them into the barcodemap_list dictionary to be split and processed based on input parameter.
def load_barcodes(parms):
    logging.debug('load_barcodes')
    global barcodemap_list
    global barcodes_used
    global barcodes_not_used

    #if parameter is set, put the "bad" item first on list so it will always exist in the first pass through the file handles    
    if parms['load_bad_bc']:
        barcodemap_list.append('bad')
    #if parameter is set to save merge file, put the "merge" item second on list so it will always exist in the first pass through the file handles    
    if parms['save_merge']:
        barcodemap_list.append('merge')
    
    barcodemap_file = file(os.path.join(args.directory, parms['barcodemap']),'r')
    for line in barcodemap_file:
        line=line.rstrip('\r\n')
        split_line = line.split('\t')
        if split_line[3] == 'Not Used':
            barcodes_not_used.append(split_line[1])
            if parms['load_not_used_bc']:
                barcodemap_list.append(split_line[1])
        elif split_line[3] != 'Template': #don't load header line
            barcodes_used.append(split_line[1])
            barcodemap_list.append(split_line[1])
                        
    barcodemap_file.close()

#Open a set of barcode files. The number open at a time will take operating system limits into consideration.
def open_barcode_files(parms, start):
    logging.debug('open_barcode_files')         
    global barcode_files
    
    end = start + fh_limit
    if end > len(barcodemap_list):
        end = len(barcodemap_list)
        
    logging.info('Open Files Batch from %i to %i.' % (start, (end-1)))
        
    for barcode in barcodemap_list[start:end]:
        barcode_files[barcode] = file(os.path.join(parms['resultsDir'], "split", barcode + '.reads'),'w')
 

#For each set of fastq reads and barcodes, merge the reads.      
def loop_pairs(parms, first_pass):
    logging.debug('loop_pairs')
    global total_reads
    global reads_with_bc_not_used
    global reads_with_bc_not_found

    uidLen = parms['uidLength']
    readFiles, barcodeFiles = load_input_filenames(parms)

    current_file = 0

    while current_file < len(readFiles):

        try :          
            reads = open_file(os.path.join(args.directory, readFiles[current_file]))
            indexes = open_file(os.path.join(args.directory, barcodeFiles[current_file]))
 
            logging.info('MERGE PASS STARTED for %s', readFiles[current_file])   
            reads_in_file = 0
            i = 0       
    
            #Process all the reads in the fastq reads file one read at a time.  
            #As a line is read from the fastq reads file, the corresponding 
            #index is read.
            for read, idx in itertools.izip(reads, indexes):
                read = read.strip() 
                idx = idx.strip()
                i += 1
                if i == 1: #we are looking at the headers from the read and index; they must match
                    read_header = read
                    index_header = idx
                elif i == 2:
                    barcode = idx
                    UidSequence = read[0: uidLen]
                    ReadSequence = read[uidLen: len(read)]

                elif i == 4:
                    index_quality = idx
                    UidQuality = read[0: uidLen]
                    ReadQuality = read[uidLen: len(read)]
            
                    reads_in_file += 1

                    i = 0
                    if read_header[0: read_header.index(" ")] == index_header[0: index_header.index(" ")] and len(ReadSequence) == len(ReadQuality):
                        merged_read_line = "\t".join([read_header, ReadSequence, ReadQuality, barcode, index_quality, UidSequence, UidQuality])+'\n'

                        #only write the reads with barcodes for this subset of open filehandles
                        if barcode in barcode_files:
                            barcode_files[barcode].write(merged_read_line)
                            
                        if first_pass: # only count records on the first pass
                            total_reads += 1

                            if barcode in barcodes_not_used:
                                reads_with_bc_not_used.append(barcode)
                            elif barcode not in barcodes_used:
                                reads_with_bc_not_found.append(barcode)
                                if 'bad' in barcode_files:
                                    barcode_files['bad'].write(merged_read_line)
                                    
                            if 'merge' in barcode_files:
                                barcode_files['merge'].write(merged_read_line)
                      
                    else:
                        logging.error('Mismatch error with read at counter %s' %(str(total_reads)))
                    
                    if total_reads % 1000000 == 0:
                        logging.info(' merged %s reads', str(total_reads))
                        print(' merged:' + str(total_reads))
                        
                    #temporary break for output size
                    #if total_reads==100:
                    #    break
                        
            reads.close()
            indexes.close()
            logging.info("  Merged %s reads." %(str(reads_in_file)))
            logging.info('MERGE PASS COMPLETED for %s', readFiles[current_file])
            current_file+=1
        
        except Exception as err:
            logging.exception(err)
            traceback.print_exc()
            sys.exit(1)


def load_input_filenames(parms):
    logging.debug('load_input_filenames')
    readFiles = []
    barcodeFiles = []
    #if user sent a format for input files
    if parms['reads_pattern'] != '' or parms['barcodes_pattern'] != '':
        #get a list of the files in the input directory
        file_list = os.listdir(args.directory)

        # Build readFiles and barcodeFiles lists from the files in the input directory
        for filename in file_list:
            if parms['reads_pattern'] in filename and '.fastq' in filename:
                readFiles.append(filename)
            elif parms['barcodes_pattern'] in filename and '.fastq' in filename:
                barcodeFiles.append(filename)
                
        #sort both finished lists
        readFiles.sort()
        barcodeFiles.sort()  
              
        #ensure that reads files were found
        if len(readFiles) == 0:
            raise Exception('No reads files found in input directory.')
        
        #ensure that barcodes files were found
        if len(barcodeFiles) == 0:
            raise Exception('No barcode files found in input directory.')
        
        #ensure there are the same number of reads files and barcodes files
        if len(readFiles) != len(barcodeFiles):
            raise Exception('Mismatch on number of read files and barcode files.')
        else:
            #ensure that the read and barcode filenames match exactly except for pattern
            for i in range(len(readFiles)):
                rf_pat_start = readFiles[i].find(parms['reads_pattern'])
                bc_pat_start = barcodeFiles[i].find(parms['barcodes_pattern'])
                rf_after_pat = rf_pat_start + len(parms['reads_pattern'])
                bc_after_pat = bc_pat_start + len(parms['barcodes_pattern'])
                if (readFiles[i][0:rf_pat_start]!= barcodeFiles[i][0:bc_pat_start] or
                    readFiles[i][rf_after_pat:]!= barcodeFiles[i][bc_after_pat:]):
                    
                    raise Exception('Read file ' + readFiles[i] + '  does not match a Barcode File.')
    else:
        readFiles = parms['reads_files']
        barcodeFiles = parms['barcodes_files']

    return readFiles, barcodeFiles


#Opens a file handle using the file extension to open the either
#a regular text file or .gz compressed file.
def open_file(filepath):
    logging.debug('open_file')

    if filepath.endswith(".gz"):
        fh = io.BufferedReader(gzip.GzipFile(filepath, "r"))
    else:
        fh = open(filepath, 'rb')
    return fh

 
def write_split_stats(parms):    
    logging.debug('write_split_stats')
    logging.info('Total Reads: %i' % total_reads)    
    logging.info('Number of Split Barcodes files: %i' % len(barcodemap_list))    
    logging.info('Barcodes in Map file listed as Used: %i' % len(barcodes_used))    
    logging.info('Barcodes in Map file listed as Not Used: %i' % len(barcodes_not_used))
    logging.info('Reads with Barcodes in Map file but listed as Not Used: %i' % len(reads_with_bc_not_used))
    logging.info('Reads with Barcodes in NOT IN Map file: %i' % len(reads_with_bc_not_found))

def run_sort_unique(parms):
    if skip_step('unique'):
        return
    
    logging.info('Unique Started.')
    print('Unique Started.')

    current_file = 0
    workers=[]

    while True:
        #check for workers that just finished.
        for p in workers:
            #exitcode is None if worker still running
            if p.exitcode is None:
                continue
            elif p.exitcode == 0:
                #Worker finished successfully, checkpoint the step completion and remove the worker
                record_checkpoint('unique', p.name )
                logging.info('   Unique completed for PID: %s' % str(p.pid))
                print ('   Unique completed for ' + p.name)
                workers.remove(p)
                break
            else:
                #process finished but had error
                logging.info('Unique failed for PID: %s' % str(p.pid))
                raise Exception('Unique Worker Failed')
                 
        #if there are still files to process 
        if current_file < len(barcodemap_list):
            #check to see if the read file was completed on a previous run.
            if barcodemap_list[current_file] == 'bad' or barcodemap_list[current_file] == 'merge' or is_done('unique', str(barcodemap_list[current_file])):
                current_file+=1
                continue
            
            #There is work to do, see if we are allowed to start another worker
            if len(multiprocessing.active_children()) < args.workers:
                read = os.path.join(parms['resultsDir'], "split", barcodemap_list[current_file]+'.reads')
                #make sure the /unique directory exists in the results directory
                unique_directory = os.path.join(parms['resultsDir'], "unique")
                if not os.path.isdir(unique_directory):
                    os.makedirs(unique_directory)
                result_file = os.path.join(unique_directory, barcodemap_list[current_file]+'.unique')
                #make sure the /family directory exists in the results directory
                family_directory = os.path.join(parms['resultsDir'], "family")
                if not os.path.isdir(family_directory):
                    os.makedirs(family_directory)
                family_file = os.path.join(family_directory, barcodemap_list[current_file]+'.family')
                
                primerset_file = os.path.join(args.directory, 'Primers.txt')                 
                
                unique_args = Namespace(input=read, output=result_file, family=family_file, primerset=primerset_file)
               
                p = multiprocessing.Process(target=unique.perform_unique, name=barcodemap_list[current_file], args=(unique_args,))
                p.start()
                logging.info('   Running unique for %s in PID: %s' %(read, str(p.pid)))
                print('   Running unique for ' + read)
                workers.append(p)
                current_file+=1
        
        #if we are finished all files, stop
        if current_file == len(barcodemap_list) and len(workers) == 0:
            break
        else: 
            time.sleep(1)    
    
    logging.info('Unique finished.')
    print('Unique finished.')

 
def main():
    try :
        get_args()
        parms = getSAFESEQSParams()
        fh_limit = set_fh_limit()
    
        #create directory for analysis results
        parms['resultsDir'] = os.path.join(args.directory, args.runname)
        if not os.path.isdir(parms['resultsDir']):
            os.makedirs(parms['resultsDir'])
        
        log_directory = os.path.join(parms['resultsDir'], "log")
        if not os.path.isdir(log_directory):
            os.makedirs(log_directory)
    
        global logfile
        logfile = os.path.join(args.directory, args.runname, "log", "SafeSeqS.log")
        logging.basicConfig(
            format='%(asctime)s %(levelname)s: %(message)s', 
            datefmt='%m/%d/%y %I:%M:%S %p',
            filename=logfile,
            level=logging.INFO)
        
        logging.info('PROCESSING STARTED')
        print('PROCESSING STARTED')
        
        #split the merged inputs into separate files for each barcode
        split_inputs(parms)
        
        run_sort_unique(parms)
             
        logging.info('PROCESSING COMPLETE')
        print 'PROCESSING COMPLETE'
    except StopStep as err:
        sys.exit(0)
    except Exception as err:
        logging.exception(err)
        traceback.print_exc()
        sys.exit(1)

if __name__ == '__main__':
    main()

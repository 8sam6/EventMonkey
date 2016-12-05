#!/usr/bin/env python
# -*- coding: utf-8 -*-
import sys
import logging
import os
import multiprocessing
import json
import re
import hashlib
import datetime
import base64
import yaml

import pyevtx
import pyevt

import XmlHandler
import ProgressManager
import DbHandler
import elastichandler
import Config
import IpManager

WINEVENT_LOGGER = logging.getLogger('WinEvent')
WINEVENT_MAPPING_FILE = 'etc/evtx.mapping.json'
DESCRIPTION_FOLDER = 'etc/descriptions'
GEODB = 'geodb'

EVENT_ID_DESCRIPTIONS = {}

WINEVENT_COLUMN_ORDER = [
    'we_hash_id',   #Hash of xml event string
    'we_index',
    'we_source',    #Source filename
    'we_jrec',      #Json Record
    'we_tags',
    'we_description',
    'eventfile_type',
    'computer_name',
    'event_category',
    'event_identifier',
    'event_identifier_qualifiers',
    'event_level',
    'identifier',
    'offset',
    'source_name',
    'strings',
    'user_security_identifier',
    'creation_time',
    'written_time',
    'xml_string',
    'data',
    'recovered',
    'we_ip_info'
]

WINEVENT_FIELD_MAPPING = {
    'we_hash_id':'CHAR(32)',
    'we_index':'BIGINT UNSIGNED',
    'we_source':'TEXT',
    'we_jrec':'JSON',
    'we_tags':'TEXT',
    'we_description':'TEXT',
    'eventfile_type':'CHAR(4)',
    'computer_name':'TEXT',
    'event_category':'BIGINT UNSIGNED',
    'event_identifier':'BIGINT UNSIGNED',
    'event_identifier_qualifiers':'BIGINT UNSIGNED',
    'event_level':'INT UNSIGNED',
    'identifier':'BIGINT UNSIGNED',
    'offset':'BIGINT UNSIGNED',
    'source_name':'TEXT',
    'strings':'TEXT',
    'user_security_identifier':'TEXT',
    'creation_time':'DATETIME',
    'written_time':'DATETIME',
    'xml_string':'BLOB',
    'data':'BLOB',
    'recovered':'INT',
    'we_ip_info':'JSON'
}

def DescriptionLoader(EVENT_ID_DESCRIPTIONS):
    if not os.path.isdir(DESCRIPTION_FOLDER):
        raise Exception('Description folder is not a directory: {}'.format(DESCRIPTION_FOLDER))
    
    # Open Descriptions Folder #
    for filename in os.listdir(DESCRIPTION_FOLDER):
        fullname = os.path.join(
            DESCRIPTION_FOLDER,
            filename
        )
        
        #That ends with .yml#
        if filename.endswith('.yml'):
            channel,file_extension = os.path.splitext(filename)
            with open(fullname,'rb') as fh:
                descriptions = yaml.load(fh)
                EVENT_ID_DESCRIPTIONS[channel] = descriptions
                fh.close()

DescriptionLoader(EVENT_ID_DESCRIPTIONS)

def Main():
    DEBUG_FILE = sys.argv[1]
    RECORD_INDEX = sys.argv[2]
    RECOVERED = sys.argv[3]
    
    if DEBUG_FILE.lower().endswith('.evt'):
        evfile = pyevt.file()
        evfile.open(DEBUG_FILE)
    elif DEBUG_FILE.lower().endswith('.evtx'):
        evfile = evtxfile.file()
        evfile.open(DEBUG_FILE)
    else:
        print u'File needs .evt or .evtx extention. {}'.format(DEBUG_FILE)
        sys.exit(1)
        
    if RECOVERED:
        record = evtxfile.get_record(RECORD_INDEX)
    else:
        record = evtxfile.get_recovered_record(RECORD_INDEX)
    
    try:
        xml_string = record.xml_string
    except:
        print u'Record has no xml_string'
    
    list_names = [
        'Event.EventData.Data',
        'Event.EventData.Binary',
    ]
    
    drec = XmlHandler.GetDictionary(
        xml_string,
        force_list=list_names
    )
    
    print drec
    
class EvtXtractFile(object):
    def __init__(self):
        self.fullname = ''
        self.filehandle = None
    
    def open(self,fullname):
        self.fullname = fullname
        self.filehandle = open(self.fullname,'rb')
        self.file = json.load(self.filehandle)
        self.records = self.file["valid_records"]
        
        pass
    
class WindowsEventManager():
    '''Handle process management of event processing'''
    def __init__(self,options):
        self.options = options
        self.total_records = 0
        
        self._GetEventFileList()
        
        self._InitOutpath()
        self._InitDb()
        self._InitEsIndex()
        
    def _InitDb(self):
        self.options.db_name = os.path.join(
            self.options.output_path,
            self.options.evidencename+'.db'
        )
        
        dbConfig = DbHandler.DbConfig(
            db_type = 'sqlite',
            db = self.options.db_name
        )
        
        dbHandler = dbConfig.GetDbHandle()
        
        dbHandler.DropTable('winevent')
        
        dbHandler.CreateTableFromMapping(
            'winevent',
            WINEVENT_FIELD_MAPPING,
            'PRIMARY KEY (we_hash_id)',
            WINEVENT_COLUMN_ORDER
        )
    
    def _InitOutpath(self):
        '''Create output path if not exists'''
        try:
            os.makedirs(self.options.output_path)
        except OSError as e:
            # Output already exists
            pass
        except Exception as e:
            WINEVENT_LOGGER.error('{}'.format(str(e)))
        
    def _InitEsIndex(self):
        '''Initialize the Elastic Index'''
        if self.options.eshost is not None:
            self.options.index_name = GetIndexName(
                self.options.evidencename
            )
            
            es_options = elastichandler.GetEsOptions(
                self.options
            )
            
            esConfig = elastichandler.EsConfig(
                es_options
            )
            
            esHandler = esConfig.GetEsHandler()
            
            result = esHandler.CheckForIndex(
                self.options.index_name
            )
            
            if result == False:
                esHandler.InitializeIndex(
                    index=self.options.index_name
                )
                
            #Check if mapping exists#
            result = esHandler.CheckForMapping(
                'winevent',
                index=self.options.index_name
            )
            
            if result == False:
                index_mapping = None
                with open(WINEVENT_MAPPING_FILE,'rb') as evtmap:
                    index_mapping = json.load(evtmap)
                    
                esHandler.InitializeMapping(
                    'winevent',
                    index_mapping,
                    index=self.options.index_name
                )
    
    def _GetEventFileList(self):
        '''Get file listing of event files from specified source path'''
        self.filelist = []
        
        for dirName, subdirList, fileList in os.walk(self.options.events_path):
            for filename in fileList:
                fullname = os.path.join(
                    dirName,
                    filename
                )
                if (filename.lower().endswith('.evt') or filename.lower().endswith('.evtx')):
                    self.filelist.append(fullname)
                elif filename.lower().endswith('.json'):
                    # Check for EvtXtract outputfiles #
                    if IsSupportedEvtXtractFile(fullname):
                        self.filelist.append(fullname)
                        
        self.filelist.sort()
        
        progressBar = ProgressManager.ProgressBarClass(
            Config.Config.UI_TYPE,
            count = len(self.filelist),
            description = u'Enumerating Event Files'.format(dirName)
        )
        
        _fcnt = 0
        for filename in self.filelist:
            if filename.lower().endswith('evtx'):
                wefile = pyevtx.file()
                wefile.open(filename)
                self.total_records += wefile.get_number_of_records()
                self.total_records += wefile.get_number_of_recovered_records()
                wefile.close()
            elif filename.lower().endswith('evt'):
                wefile = pyevt.file()
                wefile.open(filename)
                self.total_records += wefile.get_number_of_records()
                self.total_records += wefile.get_number_of_recovered_records()
                wefile.close()
            elif filename.lower().endswith('json'):
                with open(filename) as wefile:
                    jstruct = json.load(wefile)
                    self.total_records += len(jstruct['valid_records'])
                    wefile.close()
            
            progressBar.Increment(1)
            _fcnt += 1
            
        progressBar.Finish()
        
    def ProcessEvents(self):
        '''Process event log files'''
        print u'Total Records = {}'.format(self.total_records)
        # filelist_str = u''
        # for filename in self.filelist:
        #     filelist_str += filename + u"\n"
        # print u"Files to be processed:\n{}".format(filelist_str)
        
        #Progress Manager#
        progressManager = ProgressManager.ProgressManager()
        progressManager.start()
        progressBar = progressManager.ProgressBar(
            Config.Config.UI_TYPE,
            count = self.total_records,
            description = u'Processing Event Files'
        )
        if self.options.threads_to_use > 1:
            #Check to make sure enough files for all threads#
            if len(self.filelist) < self.options.threads_to_use:
                self.options.threads_to_use = len(self.filelist)
            
            #List to hold processes#
            processes = []
            c = 0
            
            #Iterate filenames for parsing#
            for filename in self.filelist:
                #Check if max threads are running#
                while len(processes) >= self.options.threads_to_use:
                    index_list = [] #hold indexes to delete
                    #Wait till process frees up#
                    for i in range(len(processes)):
                        #If process has finished
                        result = processes[i].is_alive()
                        if result == False:
                            #Terminate process cleanly i guess...
                            processes[i].terminate()
                            
                            index_list.append(i)
                            
                    for i in sorted(index_list, key=int, reverse=True):
                        del processes[i]
                
                #Add process#
                weHandler = WindowsEventHandler(
                    filename,
                    self.options,
                    progressBar
                )
                worker = multiprocessing.Process(
                    target=WindowsEventHandler.ProcessRecords,
                    args=(weHandler,)
                )
                worker.start()
                #add running process to list#
                processes.append(
                    worker
                )
                
            #Wait till all process have finished#
            while len(processes) > 0:
                for i in range(len(processes)):
                    try:
                        if not processes[i].is_alive():
                            processes[i].terminate()
                            del processes[i]
                    except:
                        pass
        else:
            for filename in self.filelist:
                name = os.path.basename(filename)
                eventHandler = WindowsEventHandler(
                    filename,
                    self.options,
                    progressBar
                )
                eventHandler.ProcessRecords()
                
        progressBar.Finish()
    
class WindowsEventHandler():
    '''Handle operations for an event file'''
    def __init__(self,filename,options,progressBar):
        '''Initialize the Event Handler for processing an event file'''
        self.filename = filename
        self.options = options
        self.ext = os.path.splitext(self.filename)[1]
        self.eventfile_type = None
        self.progressBar = progressBar
        
    def _OpenFile(self):
        '''Open the WindowsEventHandler's evt or evtx file handle for processing'''
        if self.ext.lower().endswith('evtx'):
            self.eventfile_type = 'evtx'
            self.file = pyevtx.file()
        elif self.ext.lower().endswith('evt'):
            self.eventfile_type = 'evt'
            self.file = pyevt.file()
        elif self.ext.lower().endswith('json'):
            self.eventfile_type = 'evtxtract'
            self.file = EvtXtractFile()
        else:
            raise Exception('{} Is not a supported extention. (.evt || .evtx || .json [EVTXtract json file]) [{}]'.format(
                self.ext,
                self.filename
            ))
        
        self.file.open(self.filename)
        
    def ProcessRecords(self):
        '''Process Records'''
        #Get current PID#
        pid = os.getpid()
        bname = os.path.basename(self.filename)
        WINEVENT_LOGGER.info("[PID: {}][starting] Processing: {}".format(
            pid,self.filename
        ))
        
        # Open object file handle #
        self._OpenFile()
        
        esHandler = None
        elastic_actions = []
        
        options = self.options
        
        # Create DB Handler
        dbConfig = DbHandler.DbConfig(
            db_type = 'sqlite',
            db = options.db_name
        )
        dbHandler = dbConfig.GetDbHandle()
        
        # Ip Handler #
        ipHandler = IpManager.IpHandler()
        ipHandler.AttachGeoDbs(GEODB)
        
        # Create Elastic Handler
        if options.eshost is not None:
            es_options = elastichandler.GetEsOptions(
                options
            )
            esConfig = elastichandler.EsConfig(
                es_options
            )
            esHandler = esConfig.GetEsHandler()
        
        if self.eventfile_type == 'evtx' or self.eventfile_type == 'evt':
            if len(self.file.records) == 0:
                WINEVENT_LOGGER.info("[PID: {}] {} has no records.".format(
                    pid,self.filename
                ))
            else:
                HandleRecords(
                    ipHandler,
                    self.filename,
                    options,
                    self.eventfile_type,
                    self.file.records,
                    False, #recovered flag
                    dbHandler,
                    elastic_actions,
                    self.progressBar
                )
            
            if self.file.number_of_recovered_records == 0:
                WINEVENT_LOGGER.debug("[PID: {}] {} has no recovered records.".format(
                    pid,
                    self.filename
                ))
            else:
                HandleRecords(
                    ipHandler,
                    self.filename,
                    options,
                    self.eventfile_type,
                    self.file.recovered_records,
                    True, #recovered flag
                    dbHandler,
                    elastic_actions,
                    self.progressBar
                )
        elif self.eventfile_type == 'evtxtract':
            if len(self.file.records) == 0:
                WINEVENT_LOGGER.info("[PID: {}] {} has no records.".format(
                    pid,self.filename
                ))
            else:
                HandleRecords(
                    ipHandler,
                    self.filename,
                    options,
                    self.eventfile_type,
                    self.file.records,
                    True, #recovered flag
                    dbHandler,
                    elastic_actions,
                    self.progressBar
                )
        
        #Index Elastic Records#
        if options.eshost is not None:
            esHandler.BulkIndexRecords(
                elastic_actions
            )
        
        WINEVENT_LOGGER.info("[PID: {}][finished] Processing: {}".format(
            pid,self.filename
        ))
        
def GetIpAdressInfo(ipHandler,xml_event):
    info_list = []
    for match in re.finditer('((?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?))',xml_event):
        ip_adress = match.group(1)
        info = ipHandler.GetIpInfo(ip_adress)
        info_list.append(info)
        
    if len(info_list) == 0:
        return None
    
    return info_list
    
def IsSupportedEvtXtractFile(fullname):
    result = False
    
    with open(fullname) as fh:
        jcontents = json.load(fh)
        if ("generator" in jcontents) and ("version" in jcontents) and ("valid_records" in jcontents):
            # possible EvtXtract output file #
            if jcontents['generator'] == "recover-evtx" and jcontents['version'] == 1:
                # EvtXtract version is supported #
                result = True
            else:
                print(u"{} EvtXtract format not supported".format(
                    fullname
                ))
                WINEVENT_LOGGER.warning(u"{} EvtXtract format not supported. generator: {}; version: {}".format(
                    fullname,
                    jcontents['generator'],
                    jcontents['version']
                ))
    
    return result
        
def HandleRecords(ipHandler,filename,options,eventfile_type,record_list,recovered,dbHandler,elastic_actions,progressBar):
    pid = os.getpid()
    sql_records = []
    
    recovered_flag = 0
    if recovered:
        recovered_flag = 1
    
    for i in range(len(record_list)):
        progressBar.Increment(1)
        try:
            record = record_list[i]
        except Exception as error:
            WINEVENT_LOGGER.error("[PID: {}][{}] record index {}\tERROR: {}-{}\tRecovered: {}\tNot able to get record.".format(
                pid,
                filename,
                i,
                str(type(error)),
                str(error),
                str(recovered)
            ))
            continue
        
        #Get task id if exists for debugging#
        taskid = None
        
        #########################################################################################################
        ## XML Handling
        #########################################################################################################
        #if evtx, check xml string#
        xml_string = None
        jrec = None
        drec = None
        if eventfile_type == 'evtx':
            try:
                xml_string = record.xml_string
                #Strip null values just incase
                xml_string = xml_string.strip(b'\0')
            except Exception as error:
                WINEVENT_LOGGER.warn("[PID: {}][{}] record index {}, event_id {}\tWARN: {}-{}\tRecovered: {}\tNot able to get xml string.".format(
                    pid,
                    filename,
                    i,
                    record.identifier,
                    str(type(error)),
                    str(error),
                    str(recovered)
                ))
                xml_string = None
                
            if xml_string is not None:
                list_names = [
                    'Event.EventData.Data',
                    'Event.EventData.Binary',
                ]
                drec = XmlHandler.GetDictionary(xml_string,force_list=list_names)['Event']
                jrec = json.dumps(drec)
                try:
                    taskid = drec['System']['Task']
                except:
                    WINEVENT_LOGGER.debug('[PID: {}][{}] No Task ID for record at index {} (Recovered: {})'.format(pid,filename,i,str(recovered)))
        elif eventfile_type == 'evtxtract':
            # xml stirng is excaped, we need to decode it #
            xml_string = record['xml']
            list_names = [
                'Event.EventData.Data',
                'Event.EventData.Binary',
            ]
            # xml stirng is excaped, we need to decode it #
            drec = XmlHandler.GetDictionary(record['xml'].decode('string_escape'),force_list=list_names)['Event']
            jrec = json.dumps(drec)
        #########################################################################################################
        
        # Get Ip Info #
        ip_info = GetIpAdressInfo(ipHandler,xml_string)
        if ip_info:
            ip_info = json.dumps(ip_info)
        
        rdic = {}
        rdic['eventfile_type']=eventfile_type
        
        if recovered:
            # If the record is recovered but corrupt, we should try getting as many
            # attributes as possible
            try:
                rdic['computer_name']=getattr(record,'computer_name',None)
            except:
                rdic['computer_name']=None
            try:
                rdic['creation_time']=getattr(record,'creation_time',None)
            except:
                rdic['creation_time']=None
            try:
                rdic['data']=getattr(record,'data',None)
            except:
                rdic['data']=None
            try:
                rdic['event_category']=getattr(record,'event_category',None)
            except:
                rdic['event_category']=None
            try:
                rdic['event_identifier']=getattr(record,'event_identifier',None)
            except:
                rdic['event_identifier']=None
            try:
                rdic['event_identifier_qualifiers']=getattr(record,'event_identifier_qualifiers',None)
            except:
                rdic['event_identifier_qualifiers']=None
            try:
                rdic['event_level']=getattr(record,'event_level',None)
            except:
                rdic['event_level']=None
            try:
                rdic['identifier']=getattr(record,'identifier',None)
            except:
                rdic['identifier']=None
            try:
                rdic['offset']=getattr(record,'offset',None)
            except:
                rdic['offset']=None
            try:
                rdic['source_name']=getattr(record,'source_name',None)
            except:
                rdic['source_name']=None
            try:
                rdic['user_security_identifier']=getattr(record,'user_security_identifier',None)
            except:
                rdic['user_security_identifier']=None
            try:
                rdic['written_time']=getattr(record,'written_time',None)
            except:
                rdic['written_time']=None
        else:
            rdic['computer_name']=getattr(record,'computer_name',None)
            rdic['creation_time']=getattr(record,'creation_time',None)
            
            rdic['data']=getattr(record,'data',None)
            
            rdic['event_category']=getattr(record,'event_category',None)
            rdic['event_identifier']=getattr(record,'event_identifier',None)
            rdic['event_identifier_qualifiers']=getattr(record,'event_identifier_qualifiers',None)
            rdic['event_level']=getattr(record,'event_level',None)
            rdic['identifier']=getattr(record,'identifier',None)
            rdic['offset']=getattr(record,'offset',None)
            rdic['source_name']=getattr(record,'source_name',None)
            rdic['user_security_identifier']=getattr(record,'user_security_identifier',None)
            rdic['written_time']=getattr(record,'written_time',None)
        
        rdic['strings'] = ''
        rdic['xml_string'] = xml_string
        
        c = 0
        
        if eventfile_type == 'evtx' or eventfile_type == 'evt':
            rdic['strings']=[]
            try:
                for rstring in record.strings:
                    try:
                        rdic['strings'].append(rstring)
                    except Exception as error:
                        WINEVENT_LOGGER.info("[PID: {}][{}] record index {}, id {}\tINFO: {}-{}\tRecovered: {}\tNot able to get string at index {}.".format(
                            pid,
                            filename,
                            i,
                            record.identifier,
                            str(type(error)),
                            str(error),
                            str(recovered),
                            c
                        ))
                    c+=1
                rdic['strings'] = unicode(rdic['strings'])
            except Exception as error:
                WINEVENT_LOGGER.info("[PID: {}][{}] record index {}, id {}\tINFO: {}-{}\tRecovered: {}\tNot able to iterate strings.".format(
                    pid,
                    filename,
                    i,
                    record.identifier,
                    str(type(error)),
                    str(error),
                    str(recovered)
                ))
                rdic['strings'] = None
        
        #Create unique hash#
        md5 = hashlib.md5()
        md5.update(str(rdic))
        hash_id = md5.hexdigest()
        
        we_description = None
        we_tags = None
        
        if drec is not None:
            try:
                if drec['System']['EventID']['#text'] is not None:
                    if drec['System']['Channel']['#text'] is not None:
                        try:
                            we_description = EVENT_ID_DESCRIPTIONS[unicode(drec['System']['Channel']['#text'])][int(drec['System']['EventID']['#text'])]['description']
                            we_tags = EVENT_ID_DESCRIPTIONS[unicode(drec['System']['Channel']['#text'])][int(drec['System']['EventID']['#text'])]['tags']
                            pass
                        except:
                            pass
            except:
                pass
        
        sql_insert = {
            'we_hash_id':hash_id,
            'we_source':filename,
            'we_jrec':jrec,
            'we_recovered':recovered,
            'we_index':i,
            'we_description':we_description,
            'we_tags':str(we_tags),
            'recovered':recovered_flag,
            'we_ip_info':ip_info
        }
        
        sql_insert.update(rdic)
        
        sql_records.append(sql_insert)
        
        #Add Elastic Records#
        if options.eshost is not None:
            #Add Timestamp#
            timestamp = datetime.datetime.now()
            
            # If event type is evt, make drec = rdic
            # This is because evt has no xml to make
            # into a dictionary
            if eventfile_type == 'evt' or drec is None:
                # This contains binary, which is not supported by elastic, thus
                # we need to remove it. We will encode it as base64
                dvalue = rdic.pop("data", None)
                rdic['data_printable']=getattr(record,'data',None)
                if rdic['data_printable'] is not None:
                    rdic['data_printable'] = rdic['data_printable'].decode('ascii','replace')
                if dvalue is not None:
                    rdic['data_base64']=base64.b64encode(getattr(record,'data',None))
                else:
                    rdic['data_base64']=None
                drec = rdic
            
            drec.update({
                'index_timestamp': timestamp,
                'recovered':recovered,
                'source_filename':filename,
                'index':i,
                'tags':we_tags,
                'description':we_description,
                'ip_info':ip_info
            })
            
            action = {
                "_index": options.index_name,
                "_type": 'winevent',
                "_id": hash_id,
                "_source": drec
            }
            
            elastic_actions.append(action)
        
    dbHandler.InsertFromListOfDicts(
        'winevent',
        sql_records,
        WINEVENT_COLUMN_ORDER
    )
    
def GetTags(event_id):
    pass

def GetDescription(event_id):
    pass
    
def GetIndexName(index_name):
    index_name = index_name.lower()
    index_name = CleanIndexName(index_name)
    return index_name

def CleanIndexName(index_name):
    return re.sub('[^a-zA-Z0-9-]', '-', index_name)
    
if __name__ == '__main__':
    Main()

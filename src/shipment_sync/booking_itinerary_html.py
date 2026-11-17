"""Pure verification of archived Wan Hai booking fields, never DCSA events."""
from html.parser import HTMLParser
from copy import deepcopy
from datetime import datetime
from urllib.parse import urlsplit
import hashlib,re
from shipment_sync.movement_archive import digest
FORMAT='WANHAI_BOOKING_HTML'
MAX_HTML=2_000_000
LABELS={'訂艙號碼':'booking','Booking No.':'booking','Booking Number':'booking','船名':'vessel','Vessel':'vessel',
 '航次':'voyage','Voyage':'voyage','Place of Receipt':'receipt_port','Port of Loading':'loading_port',
 'Port of Discharging':'discharge_port','Port of Discharge':'discharge_port','Estimated Departure Date':'departure_date',
 'Estimated Arrival Date':'arrival_date','Booking Status':'booking_status'}

def normal(value):
 value=re.sub(r'\s*\([A-Z]{2}\)\s*$','',str(value).upper())
 return re.sub(r'[^A-Z0-9]','',value)

class Cells(HTMLParser):
 def __init__(self):
  super().__init__(convert_charrefs=True);self.rows=[];self.row=None;self.cell=None;self.hidden=0;self.table=0;self.table_number=0;self.current_table=None;self.text=[];self.cell_id=0
 def handle_starttag(self,tag,attrs):
  tag=tag.lower()
  if tag in {'script','style','noscript'}:self.hidden+=1
  if self.hidden:return
  if tag=='table':
   self.table+=1
   if self.table==1:self.table_number+=1;self.current_table=self.table_number
  elif tag=='tr'and self.table==1:
   if self.row is not None:raise ValueError('Ambiguous nested native HTML row')
   self.row=[]
  elif tag in {'td','th'}and self.table==1 and self.row is not None:
   if self.cell is not None:raise ValueError('Ambiguous native HTML cell')
   self.cell=[]
 def handle_endtag(self,tag):
  tag=tag.lower()
  if tag in {'script','style','noscript'}:
   self.hidden=max(0,self.hidden-1);return
  if self.hidden:return
  if tag in {'td','th'}and self.cell is not None and self.table==1:
   self.cell_id+=1;self.row.append({'cell':self.cell_id,'text':' '.join(' '.join(self.cell).split())});self.cell=None
  elif tag=='tr'and self.row is not None and self.table==1:
   if self.cell is not None:raise ValueError('Unclosed native HTML cell')
   self.rows.append({'table':self.current_table,'cells':self.row});self.row=None
  elif tag=='table':self.table=max(0,self.table-1)
 def handle_data(self,data):
  if self.hidden:return
  if data.strip():self.text.append(data.strip())
  if self.cell is not None:self.cell.append(data)


def page(document,hosts):
 if (not isinstance(document,dict)or document.get('kind')not in {'BOOKING_DETAIL','BOOKING_RESULT'}or
     document.get('capture_kind')!='BROWSER_DOM'or document.get('navigation_status')!=200 or document.get('complete')is not True):raise ValueError('Complete native Wan Hai browser document required')
 html=document.get('html');url=urlsplit(document.get('url',''))
 if (not isinstance(html,str)or not 0<len(html.encode())<=MAX_HTML or
     hashlib.sha256(html.encode()).hexdigest()!=document.get('sha256')or
     document.get('final_url')!=document.get('url')or url.scheme!='https'or url.hostname not in hosts or
     url.port not in (None,443)or url.username or url.password):raise ValueError('Wan Hai native page bytes or origin changed')
 parser=Cells();parser.feed(html);parser.close()
 if parser.cell is not None or parser.row is not None or parser.table:raise ValueError('Native Wan Hai HTML is incomplete')
 if any(t in html.casefold()for t in ('_incapsula_resource','access denied','please enable javascript')):raise ValueError('Wan Hai native page contains an access challenge')
 return parser


def verify(manifest,binding,approved):
 """Return a labeled booking leg, with native cells instead of invented events."""
 if manifest.get('native_format')!=FORMAT or binding.get('carrier')!='WAN HAI':raise ValueError('Wan Hai format cannot prove another carrier')
 documents=manifest.get('native_documents')
 if not isinstance(documents,list)or len(documents)!=2 or {d.get('kind')for d in documents}!={'BOOKING_DETAIL','BOOKING_RESULT'}:raise ValueError('Exact Wan Hai result and booking-detail inventory required')
 hosts=approved['native_hosts'][binding['carrier']];parsed={d['kind']:page(d,hosts)for d in documents}
 detail=next(d for d in documents if d['kind']=='BOOKING_DETAIL');parser=parsed['BOOKING_DETAIL']
 if 'Tracking Information Content [By Book No.]'not in ' '.join(parser.text):raise ValueError('Native Wan Hai booking-detail title unavailable')
 values={};evidence={}
 for row in parser.rows:
  cells=row['cells']
  for index,cell in enumerate(cells):
   label=cell['text'].strip().rstrip(':').strip();key=LABELS.get(label)
   if not key:continue
   if key in values or index+1>=len(cells):raise ValueError('Missing or duplicate native Wan Hai field')
   value=cells[index+1]['text']
   if not value or value in LABELS:raise ValueError('Native Wan Hai field has no exact value')
   values[key]=value;evidence[key]={'document_sha256':detail['sha256'],'table':row['table'],
    'label_cell':cell['cell'],'value_cell':cells[index+1]['cell'],'label':label,'value':value}
 if any(not values.get(k)for k in ('booking','vessel','voyage','loading_port','discharge_port'))or values['booking']!=binding['booking_reference']:raise ValueError('Native Wan Hai identity or required booking fields incomplete')
 if normal(values['discharge_port'])!=normal(binding.get('destination_port_label','')):raise ValueError('Native Wan Hai destination differs from current shipment')
 # Result table must independently include the exact requested booking cell.
 if not any(cell['text']==binding['booking_reference']for row in parsed['BOOKING_RESULT'].rows for cell in row['cells']):raise ValueError('Native Wan Hai result does not match requested booking')
 units=[];header_tables=set()
 for row in parser.rows:
  if row['cells']and normal(row['cells'][0]['text'])in {'CTNRNO','CONTAINERNO','CONTAINERNUMBER'}:header_tables.add(row['table']);continue
  if row['table']in header_tables and row['cells']:
   value=row['cells'][0]['text'].strip().upper()
   if value:
    if not re.fullmatch(r'[A-Z]{4}\d{7}',value):raise ValueError('Ambiguous native Wan Hai container table')
    units.append(value)
 if len(units)!=len(set(units)):raise ValueError('Duplicate native Wan Hai container assignment')
 if binding.get('assigned_containers')and set(units)!=set(binding['assigned_containers']):raise ValueError('Wan Hai booking container allocation differs from current shipment')
 dates={}
 for key in ('departure_date','arrival_date'):
  value=values.get(key)
  if value:
   if not re.fullmatch(r'20\d\d[/-]\d{2}[/-]\d{2}',value):raise ValueError('Wan Hai date needs an unambiguous native calendar date')
   dates[key]=datetime.strptime(value.replace('/','-'),'%Y-%m-%d').date().isoformat()
 result={'native_format':FORMAT,'booking_reference':binding['booking_reference'],
  'loading_port':values['loading_port'],'receipt_port':values.get('receipt_port'),
  'discharge_port':values['discharge_port'],'vessel':values['vessel'],'voyage':values['voyage'],
  **dates,'date_precision':'CALENDAR_DATE','classifier':'ESTIMATED_BOOKING_SCHEDULE',
  'observed_container_assignments':sorted(units),'field_evidence':evidence,
  'native_document_hashes':{d['kind']:d['sha256']for d in documents},'not_a_dcsa_event':True,
  'container_movements_completed':False,'source_field_writes':0}
 result['leg_proof_sha256']=digest(result)
 return result

"""Create a local lake CSV manifest for synthetic geofence shipments."""
import csv,hashlib,json
from pathlib import Path
root=Path(__file__).resolve().parent
file=root/'shipments.csv'
with file.open('w',newline='',encoding='utf-8') as f:
    w=csv.writer(f,lineterminator='\n');w.writerow(['shipment_id','longitude','latitude','dt']);w.writerows([['S1','0','82','2024-01-01'],['S2','178.5','10','2024-01-01'],['S3','179.5','10','2024-01-01']])
entry={'path':file.name,'partition':'dt=2024-01-01','size':file.stat().st_size,'row_count':3,'sha256':hashlib.sha256(file.read_bytes()).hexdigest()}
manifest={'table':'shipments','snapshot_id':'snap-geo-0001','parent_snapshot_id':None,'sequence':1,'created_at':'2024-01-01T00:00:00Z','schema':[{'name':'shipment_id','type':'STRING','nullable':False},{'name':'longitude','type':'DECIMAL','nullable':False},{'name':'latitude','type':'DECIMAL','nullable':False},{'name':'dt','type':'STRING','nullable':False}],'partition_spec':['dt'],'files':[entry],'stats':{}}
(root/'shipments-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
contract={'schema_version':1,'shipments_manifest':'fixtures/geofence/shipments-manifest.json','zones':'fixtures/geofence/zones.json','contracts':['geodesic_edges','rhumb_edges'],'boundary_rule':'inside_outer_outside_holes'}
(root/'contract.json').write_text(json.dumps(contract,indent=2)+'\n',encoding='utf-8')


"""Build three local CSV/manifest snapshots with a true single-parent fork."""
import csv,hashlib,json
from pathlib import Path
root=Path(__file__).resolve().parent
snapshots={
    'S0': [('O1',100,'hold'),('O2',50,'open')],
    'S1': [('O1',120,'hold'),('O2',50,'open')],
    'S2': [('O1',100,'approved'),('O2',50,'open')],
}
for sid,rows in snapshots.items():
    file=root/f'orders-{sid}.csv'
    with file.open('w',newline='',encoding='utf-8') as f:
        w=csv.writer(f,lineterminator='\n');w.writerow(['order_id','amount','status']);w.writerows(rows)
    entry={'path':file.name,'partition':'dt=2024-01-01','size':file.stat().st_size,'row_count':len(rows),'sha256':hashlib.sha256(file.read_bytes()).hexdigest()}
    manifest={'table':'orders','snapshot_id':sid,'parent_snapshot_id':None if sid=='S0' else 'S0','sequence':{'S0':1,'S1':2,'S2':3}[sid], 'created_at':{'S0':'2024-01-01T00:00:00Z','S1':'2024-01-02T00:00:00Z','S2':'2024-01-03T00:00:00Z'}[sid], 'schema':[{'name':'order_id','type':'STRING','nullable':False},{'name':'amount','type':'LONG','nullable':False},{'name':'status','type':'STRING','nullable':False}], 'partition_spec':['dt'],'files':[entry], 'stats':{}}
    (root/f'{sid}.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
contract={'ancestor':'fixtures/fork_merge/S0.json','primary':'fixtures/fork_merge/S1.json','replica':'fixtures/fork_merge/S2.json','contracts':['file_strict','field_disjoint'],'expected_merge_parents':['S1','S2']}
(root/'contract.json').write_text(json.dumps(contract,indent=2)+'\n',encoding='utf-8')

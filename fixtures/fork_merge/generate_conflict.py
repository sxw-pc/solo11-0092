"""Generate a deletion-versus-update sibling fork that must not auto-merge."""
import csv,hashlib,json
from pathlib import Path
root=Path(__file__).resolve().parent
for sid,rows,seq in [('S1D',[('O1',100,'hold')],2),('S2U',[('O1',100,'hold'),('O2',50,'closed')],3)]:
    file=root/f'orders-{sid}.csv'
    with file.open('w',newline='',encoding='utf-8') as f:
        w=csv.writer(f,lineterminator='\n');w.writerow(['order_id','amount','status']);w.writerows(rows)
    entry={'path':file.name,'partition':'dt=2024-01-01','size':file.stat().st_size,'row_count':len(rows),'sha256':hashlib.sha256(file.read_bytes()).hexdigest()}
    manifest={'table':'orders','snapshot_id':sid,'parent_snapshot_id':'S0','sequence':seq,'created_at':f'2024-01-0{seq}T00:00:00Z','schema':[{'name':'order_id','type':'STRING','nullable':False},{'name':'amount','type':'LONG','nullable':False},{'name':'status','type':'STRING','nullable':False}],'partition_spec':['dt'],'files':[entry],'stats':{}}
    (root/f'{sid}.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
contract={'ancestor':'fixtures/fork_merge/S0.json','primary':'fixtures/fork_merge/S1D.json','replica':'fixtures/fork_merge/S2U.json','contracts':['file_strict','field_disjoint'],'expected_merge_parents':['S1D','S2U']}
(root/'conflict-contract.json').write_text(json.dumps(contract,indent=2)+'\n',encoding='utf-8')

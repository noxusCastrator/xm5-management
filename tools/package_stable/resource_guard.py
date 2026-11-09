"""Independent structural guard for the only changed stock UI resource."""
from pathlib import Path
import importlib.util,hashlib
HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('ui_reader',HERE/'awtk_resource.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
PATH='release/assets/default/raw/ui/playset/equalizer_page.bin'
def guard(stock,candidate):
 if stock[:4]!=m.MAGIC or candidate[:4]!=m.MAGIC:raise ValueError('UI magic')
 old,end=m.parse(stock,4);new,end2=m.parse(candidate,4)
 if end!=len(stock) or end2!=len(candidate):raise ValueError('UI trailing bytes')
 if any(old[k]!=new[k] for k in ['type','rect','props']):raise ValueError('root modified')
 if len(new['children'])!=1:raise ValueError('pager count')
 pager=new['children'][0]
 full=['self_layout','default(x=0,y=0,w=100%,h=100%)']
 expected=[['name','xm5_eq_pager'],full,['active','0'],['vertical','false'],['loop','false'],['animating_time','300']]
 if pager['type']!='slide_view' or pager['rect']!=[0,0,720,720] or pager['props']!=expected:
  raise ValueError('pager properties')
 if len(pager['children'])!=2:raise ValueError('page count')
 host,peq=pager['children']
 for node,name in [(host,'stock_eq_host'),(peq,'peq_host')]:
  if node['type']!='view' or node['rect']!=[0,0,720,720] or node['props']!=[['name',name],full]:
   raise ValueError('host properties')
 if peq['children']:raise ValueError('unexpected PEQ resource children')
 if len(old['children'])!=2 or len(host['children'])!=2:raise ValueError('stock subtree count')
 for a,b in zip(old['children'],host['children']):
  if m.encode(a)!=m.encode(b):raise ValueError('stock subtree changed')
 return {'stock_subtrees_verbatim':True,'root_unchanged':True,'native_pager':True}

import json
from pathlib import Path
from bot import compose

ROOT=Path(__file__).parent
DATA=ROOT/'expanded'


def load_dir(dirname,key):
    out={}
    for p in (DATA/dirname).glob('*.json'):
        d=json.loads(p.read_text(encoding='utf-8'))
        out[d[key]]=d
    return out

cats=load_dir('categories','slug')
merchants=load_dir('merchants','merchant_id')
customers=load_dir('customers','customer_id')
triggers=load_dir('triggers','id')
pairs=json.loads((DATA/'test_pairs.json').read_text())['pairs']

out=ROOT/'submission.jsonl'
with out.open('w',encoding='utf-8') as f:
    for pair in pairs:
        m=merchants[pair['merchant_id']]
        t=triggers[pair['trigger_id']]
        c=customers.get(pair.get('customer_id')) if pair.get('customer_id') else None
        result=compose(cats[m['category_slug']],m,t,c)
        row={'test_id':pair['test_id'], **result}
        f.write(json.dumps(row,ensure_ascii=False)+'\n')
print(out)

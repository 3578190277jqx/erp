"""Pure JSON identity/equality helpers; no database or authorization decisions."""


def same_value(a,b):
    if type(a) is not type(b):return False
    if isinstance(a,dict):return a.keys()==b.keys() and all(same_value(a[k],b[k]) for k in a)
    if isinstance(a,list):return len(a)==len(b) and all(same_value(x,y) for x,y in zip(a,b))
    return a==b


def identity(value,key):
    if key=='code':return value if isinstance(value,str) and value.strip() else None
    if isinstance(value,bool) or not isinstance(value,(str,int)):return None
    text=str(value)
    return str(int(text)) if text.isascii() and text.isdigit() and 0<int(text)<2**63 else None


def unique_keys(items,key):
    if not all(isinstance(item,dict) for item in items):return None
    values=[identity(item.get(key),key) for item in items]
    return values if all(v is not None for v in values) and len(set(values))==len(values) else None


def list_identity(before,after):
    for key in ('code','id'):
        left=unique_keys(before,key);right=unique_keys(after,key)
        if left is not None and right is not None:return key,left,right
    return None


def canonical_rows(tree,rows,stage):
    """Use actual unique identities for shared context; ambiguous lists stay side-specific."""
    result={};cache={}
    for row in rows:
        cursor=tree;path=[];labels=[]
        for component,label in zip(row['path'],row['labels']):
            if isinstance(cursor,list):
                marker=id(cursor)
                if marker not in cache:
                    cache[marker]=next((key for key in ('code','id') if unique_keys(cursor,key) is not None),None)
                key=cache[marker]
                if key:
                    value=identity(cursor[component][key],key)
                    path.append((key,value));labels.append(value if key=='code' else f'ID: {value}')
                else:path.append((stage,component));labels.append(label)
            else:path.append(component);labels.append(label)
            cursor=cursor[component]
        result[tuple(path)]=dict(row,canonical_key=' / '.join(labels) or '内容')
    return result

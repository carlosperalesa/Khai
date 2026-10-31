# MODE: SAFE
# PIP: pypdf==6.21.4 openpyxl==3.1.5 defusedxml==0.7.1
import json
from khai_input import params
from khai_documents import read_document

if set(params) != {'path'} or not isinstance(params['path'], str):
    raise ValueError('Expected path relative to skill data')
print(json.dumps(read_document(params['path']), ensure_ascii=False))

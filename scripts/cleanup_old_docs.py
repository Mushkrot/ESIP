"""
Delete old PDF documents from existing data stores.
Removes documents with URIs pointing to old folders:
  - gs://esip-reference-data/legacy_operations_articles/   (old PDFs)
  - gs://esip-reference-data/legacy_support_articles/      (old PDFs)
New HTML documents under operations_articles_v2/ and support_articles_v2/ are kept.
"""
import requests, json, os, time
from google.oauth2 import service_account
import google.auth.transport.requests
from dotenv import load_dotenv

load_dotenv(override=True)

creds = service_account.Credentials.from_service_account_file(
    os.getenv('GOOGLE_APPLICATION_CREDENTIALS'),
    scopes=['https://www.googleapis.com/auth/cloud-platform']
)

def get_token():
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token

BASE = 'https://discoveryengine.googleapis.com/v1'
COL  = 'projects/123456789012/locations/global/collections/default_collection'

STORES = [
    ('operations_articles', 'reference-operations-knowledge', 'gs://esip-reference-data/legacy_operations_articles/'),
    ('support_articles', 'reference-support-knowledge', 'gs://esip-reference-data/legacy_support_articles/'),
]

def get_all_documents(store_id, token):
    docs = []
    page_token = None
    while True:
        params = {'pageSize': 1000}
        if page_token:
            params['pageToken'] = page_token
        H = {'Authorization': f'Bearer {token}'}
        r = requests.get(
            f'{BASE}/{COL}/dataStores/{store_id}/branches/default_branch/documents',
            headers=H, params=params, timeout=30
        )
        data = r.json()
        docs.extend(data.get('documents', []))
        page_token = data.get('nextPageToken')
        if not page_token:
            break
    return docs

def delete_document(store_id, doc_id, token):
    H = {'Authorization': f'Bearer {token}'}
    r = requests.delete(
        f'{BASE}/{COL}/dataStores/{store_id}/branches/default_branch/documents/{doc_id}',
        headers=H,
        timeout=30,
    )
    return r.status_code

for store_name, store_id, old_prefix in STORES:
    print(f'\n{"="*60}')
    print(f'Processing: {store_name}')
    print(f'Removing documents from: {old_prefix}')
    print(f'{"="*60}')

    token = get_token()
    all_docs = get_all_documents(store_id, token)
    print(f'Total documents in store: {len(all_docs)}')

    old_docs = [
        d for d in all_docs
        if d.get('content', {}).get('uri', '').startswith(old_prefix)
    ]
    print(f'Old PDF documents to delete: {len(old_docs)}')

    if not old_docs:
        print('Nothing to delete.')
        continue

    deleted = 0
    errors  = 0
    for i, doc in enumerate(old_docs, 1):
        if i % 100 == 0:
            token = get_token()  # refresh token every 100 requests
        doc_id = doc['name'].split('/')[-1]
        status = delete_document(store_id, doc_id, token)
        if status == 200:
            deleted += 1
        else:
            errors += 1
        if i % 50 == 0 or i == len(old_docs):
            print(f'  {i}/{len(old_docs)} processed (deleted: {deleted}, errors: {errors})')
        time.sleep(0.05)  # be gentle with the API

    print(f'\nDone. Deleted: {deleted}, Errors: {errors}')
    print(f'Remaining documents should be: {len(all_docs) - deleted}')

print('\n\nAll done!')

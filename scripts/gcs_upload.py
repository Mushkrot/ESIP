"""
Upload exported HTML files to GCS bucket into new v2 folders.
Legacy folders are not modified.
Uses REST API directly to avoid system credential conflicts.
"""
import os
import requests
from pathlib import Path
from google.oauth2 import service_account
import google.auth.transport.requests
from dotenv import load_dotenv

load_dotenv(override=True)

CREDENTIALS_FILE = os.getenv('GOOGLE_APPLICATION_CREDENTIALS')
BUCKET_NAME      = 'esip-reference-data'
EXPORT_DIR       = Path('export')

FOLDERS = {
    'support_articles': 'support_articles_v2',
    'operations_articles': 'operations_articles_v2',
}

# Use explicit service account credentials — bypasses system ADC
creds = service_account.Credentials.from_service_account_file(
    CREDENTIALS_FILE,
    scopes=['https://www.googleapis.com/auth/cloud-platform']
)


def get_token():
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def upload_folder(local_folder, gcs_prefix):
    local_path = EXPORT_DIR / local_folder
    files = list(local_path.glob('*.html'))
    print(f'\nUploading {len(files)} files → gs://{BUCKET_NAME}/{gcs_prefix}/')

    token = get_token()
    upload_url = 'https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o'

    for i, filepath in enumerate(files, 1):
        # Refresh token every 200 files (tokens expire after 1 hour)
        if i % 200 == 0:
            token = get_token()

        blob_name = f'{gcs_prefix}/{filepath.name}'
        content = filepath.read_bytes()

        r = requests.post(
            upload_url.format(bucket=BUCKET_NAME),
            params={'uploadType': 'media', 'name': blob_name},
            headers={
                'Authorization': f'Bearer {token}',
                'Content-Type': 'text/html; charset=utf-8',
            },
            data=content,
            timeout=120,
        )
        if r.status_code not in (200, 201):
            print(f'  ERROR on {filepath.name}: {r.status_code} {r.text[:200]}')
            continue

        if i % 100 == 0 or i == len(files):
            print(f'  {i}/{len(files)} uploaded...')

    print(f'  Done.')
    return len(files)


if __name__ == '__main__':
    print('GCS Upload — new v2 folders only, old folders untouched')
    print('=' * 60)

    total = 0
    for local_folder, gcs_prefix in FOLDERS.items():
        total += upload_folder(local_folder, gcs_prefix)

    print()
    print('=' * 60)
    print(f'Done. Total uploaded: {total} files')
    print(f'Bucket: gs://{BUCKET_NAME}/')
    print(f'  Uploaded prefixes: support_articles_v2/ and operations_articles_v2/')

"""
Export published Zendesk Help Center articles to HTML files.
Organizes into two folders:
  export/support_articles/    — Support Knowledge (section 990300001)
  export/operations_articles/ — Operations Knowledge (section 990300002)
"""
import os
import re
import time
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

SUBDOMAIN = os.getenv('ZENDESK_SUBDOMAIN')
EMAIL     = os.getenv('ZENDESK_EMAIL')
TOKEN     = os.getenv('ZENDESK_API_TOKEN')
BASE      = f'https://{SUBDOMAIN}.zendesk.com/api/v2/help_center'
AUTH      = (f'{EMAIL}/token', TOKEN)

SECTIONS = {
    990300001: 'support_articles',
    990300002: 'operations_articles',
}

EXPORT_DIR = Path('export')


def slugify(text, max_len=80):
    text = text.lower()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[\s_]+', '-', text).strip('-')
    return text[:max_len]


def make_html(article):
    title      = article['title']
    body       = article.get('body') or ''
    article_id = article['id']
    url        = article['html_url']
    updated    = article.get('updated_at', '')

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{title}</title>
  <meta name="article_id" content="{article_id}">
  <meta name="source_url" content="{url}">
  <meta name="updated_at" content="{updated}">
</head>
<body>
  <h1>{title}</h1>
  <p><em>Source: <a href="{url}">{url}</a> | Updated: {updated}</em></p>
  <hr>
  {body}
</body>
</html>"""


def fetch_articles(section_id):
    articles = []
    page = 1
    while True:
        r = requests.get(
            f'{BASE}/sections/{section_id}/articles.json',
            params={'per_page': 100, 'page': page},
            auth=AUTH,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        batch = [a for a in data.get('articles', []) if not a.get('draft')]
        articles.extend(batch)
        print(f'  Page {page}: {len(batch)} articles (total so far: {len(articles)})')
        if not data.get('next_page'):
            break
        page += 1
        time.sleep(0.2)  # be polite to Zendesk API
    return articles


def export_section(section_id, folder_name):
    out_dir = EXPORT_DIR / folder_name
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f'\nExporting section → export/{folder_name}/')
    articles = fetch_articles(section_id)
    print(f'  Total published: {len(articles)}')

    saved = 0
    for article in articles:
        slug     = slugify(article['title'])
        filename = f"{article['id']}_{slug}.html"
        filepath = out_dir / filename
        filepath.write_text(make_html(article), encoding='utf-8')
        saved += 1

    print(f'  Saved: {saved} files')
    return saved


if __name__ == '__main__':
    print('Zendesk export — published articles only')
    print('=' * 60)

    total = 0
    for section_id, folder_name in SECTIONS.items():
        total += export_section(section_id, folder_name)

    print()
    print('=' * 60)
    print(f'Done. Total files saved: {total}')
    print(f'Output: {EXPORT_DIR.resolve()}')

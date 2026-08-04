import type { ReactNode } from 'react'

interface Props {
  text: string
  as?: 'span' | 'div'
  className?: string
}

const SAFE_LINK_PREFIXES = ['https://', 'http://', 'mailto:']

function isSafeHref(href: string) {
  const normalized = href.trim().toLowerCase()
  return SAFE_LINK_PREFIXES.some(prefix => normalized.startsWith(prefix))
}

function classNames(...values: Array<string | undefined>) {
  return values.filter(Boolean).join(' ')
}

function renderInlineMarkdown(text: string): ReactNode[] {
  const parts: ReactNode[] = []
  let index = 0
  let key = 0

  function pushText(value: string) {
    if (value) parts.push(value)
  }

  function pushElement(node: ReactNode) {
    parts.push(<span key={key++}>{node}</span>)
  }

  while (index < text.length) {
    const nextCode = text.indexOf('`', index)
    const nextStarBold = text.indexOf('**', index)
    const nextUnderscoreBold = text.indexOf('__', index)
    const nextLink = text.indexOf('[', index)

    const candidates = [nextCode, nextStarBold, nextUnderscoreBold, nextLink]
      .filter(position => position >= 0)
      .sort((a, b) => a - b)

    if (candidates.length === 0) {
      pushText(text.slice(index))
      break
    }

    const next = candidates[0]
    pushText(text.slice(index, next))
    index = next

    if (text[index] === '`') {
      const end = text.indexOf('`', index + 1)
      if (end > index + 1) {
        const value = text.slice(index + 1, end)
        pushElement(<code>{value}</code>)
        index = end + 1
        continue
      }
      pushText(text[index])
      index += 1
      continue
    }

    const marker = text.slice(index, index + 2)
    if (marker === '**' || marker === '__') {
      const end = text.indexOf(marker, index + 2)
      if (end > index + 2) {
        const value = text.slice(index + 2, end)
        pushElement(<strong>{value}</strong>)
        index = end + 2
        continue
      }
      pushText(marker)
      index += 2
      continue
    }

    if (text[index] === '[') {
      const labelEnd = text.indexOf(']', index + 1)
      const hrefStart = labelEnd >= 0 ? labelEnd + 1 : -1
      if (labelEnd > index + 1 && text[hrefStart] === '(') {
        const hrefEnd = text.indexOf(')', hrefStart + 1)
        if (hrefEnd > hrefStart + 1) {
          const label = text.slice(index + 1, labelEnd)
          const href = text.slice(hrefStart + 1, hrefEnd)
          if (isSafeHref(href)) {
            pushElement(
              <a
                href={href}
                target="_blank"
                rel="noreferrer noopener"
                onClick={event => event.stopPropagation()}
              >
                {label}
              </a>
            )
          } else {
            pushText(text.slice(index, hrefEnd + 1))
          }
          index = hrefEnd + 1
          continue
        }
      }
      pushText(text[index])
      index += 1
      continue
    }

    pushText(text[index])
    index += 1
  }

  return parts
}

export default function InlineMarkdownText({ text, as = 'span', className }: Props) {
  const Component = as
  return (
    <Component className={classNames('inline-markdown-text', className)}>
      {renderInlineMarkdown(text)}
    </Component>
  )
}

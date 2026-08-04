import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { PageScroll } from '@/components/PageScroll'
import guideContent from '@/content/guide.md?raw'

export function GuidePage() {
  return (
    <PageScroll className="px-4 py-4 sm:px-6">
      <div className="max-w-4xl mr-auto">
        <article className="prose max-w-none prose-sm dark:prose-invert
            prose-headings:mb-2 prose-headings:mt-4
            prose-h1:mt-0 prose-h1:mb-2 prose-h1:text-xl prose-h1:font-bold
            prose-h2:text-base prose-h2:font-semibold
            prose-h3:text-sm prose-h3:font-semibold
            prose-p:my-1.5 prose-li:my-0.5
            prose-ul:my-2 prose-ol:my-2
            prose-code:text-[0.8em]
            prose-td:py-1 prose-th:py-1">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{guideContent}</ReactMarkdown>
        </article>
      </div>
    </PageScroll>
  )
}

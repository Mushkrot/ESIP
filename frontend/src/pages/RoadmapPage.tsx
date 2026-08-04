import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { PageScroll } from '@/components/PageScroll'
import roadmapContent from '@/content/roadmap.md?raw'

export function RoadmapPage() {
  return (
    <PageScroll className="px-6 py-10">
      <div className="max-w-3xl mr-auto">
        <article className="prose dark:prose-invert max-w-none prose-h1:text-2xl prose-h1:font-bold prose-h1:text-center prose-h2:text-base prose-h2:font-semibold prose-h3:text-base prose-h3:font-semibold prose-h4:text-base prose-h4:font-semibold prose-p:text-sm prose-li:text-sm prose-td:text-sm prose-th:text-sm prose-strong:text-sm">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{roadmapContent}</ReactMarkdown>
        </article>
      </div>
    </PageScroll>
  )
}

import { useEffect, useRef, useState } from 'react'
import type { AppContext } from '../App'
import InlineMarkdownText from './InlineMarkdownText'

interface Props {
  ctx: AppContext
  backend: string
}

interface Message {
  role: 'user' | 'assistant'
  content: string
}

export default function ModalChat({ ctx, backend }: Props) {
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const conversationIdRef = useRef(`zaf-${ctx.ticketId}-${Date.now()}`)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const abortRef = useRef<AbortController | null>(null)

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  async function send() {
    const text = input.trim()
    if (!text || streaming) return

    const newMessages: Message[] = [...messages, { role: 'user', content: text }]
    setMessages(newMessages)
    setInput('')
    setStreaming(true)

    // Placeholder for assistant response
    setMessages(m => [...m, { role: 'assistant', content: '' }])

    abortRef.current = new AbortController()

    try {
      const resp = await fetch(`${backend}/api/universal/stream`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${ctx.sessionToken}`,
        },
        body: JSON.stringify({
          ticket_id: ctx.ticketId,
          conversation_id: conversationIdRef.current,
          messages: newMessages,
        }),
        signal: abortRef.current.signal,
      })

      if (!resp.ok || !resp.body) {
        setMessages(m => {
          const updated = [...m]
          updated[updated.length - 1] = { role: 'assistant', content: 'Error: could not get response.' }
          return updated
        })
        return
      }

      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })

        const lines = buffer.split('\n')
        buffer = lines.pop() ?? ''

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          try {
            const json = JSON.parse(line.slice(6)) as { delta?: string; done?: boolean; error?: string }
            if (json.error) {
              setMessages(m => {
                const updated = [...m]
                updated[updated.length - 1] = { role: 'assistant', content: `Error: ${json.error}` }
                return updated
              })
            } else if (json.delta) {
              setMessages(m => {
                const updated = [...m]
                updated[updated.length - 1] = {
                  role: 'assistant',
                  content: updated[updated.length - 1].content + json.delta,
                }
                return updated
              })
            }
          } catch {
            // skip malformed line
          }
        }
      }
    } catch (err) {
      if ((err as Error).name !== 'AbortError') {
        setMessages(m => {
          const updated = [...m]
          updated[updated.length - 1] = { role: 'assistant', content: 'Connection error.' }
          return updated
        })
      }
    } finally {
      setStreaming(false)
      abortRef.current = null
    }
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      void send()
    }
  }

  return (
    <div className="modal-chat">
      <div className="modal-chat-messages">
        {messages.length === 0 && (
          <div className="modal-chat-hint">
            Ask about this ticket: causes, solutions, similar past issues, KB articles…
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`chat-message ${m.role}`}>
            {m.role === 'user'
              ? <span className="chat-user-label">You: </span>
              : <span className="chat-ai-label">ESIP: </span>
            }
            {m.role === 'assistant'
              ? (
                <InlineMarkdownText
                  text={m.content || (streaming && i === messages.length - 1 ? '▌' : '')}
                />
              )
              : m.content
            }
          </div>
        ))}
        <div ref={messagesEndRef} />
      </div>

      <div className="modal-chat-input-row">
        <textarea
          className="modal-chat-input"
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Ask a question… (Enter to send)"
          disabled={streaming}
          rows={2}
        />
        <button
          className="chat-send-btn"
          onClick={() => void send()}
          disabled={streaming || !input.trim()}
        >
          {streaming ? '…' : 'Send'}
        </button>
      </div>
    </div>
  )
}

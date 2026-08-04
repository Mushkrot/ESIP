// Types mirroring the FastAPI backend models exactly.
// Keep in sync with backend/app/models/

export interface TicketComment {
  id: number
  author_name: string
  body: string
  is_internal: boolean
  created_at: string
}

export interface StoreContext {
  store_id: string
  org_name: string
  brand: string
  org_id: number
}

export interface TicketDetail {
  id: number
  subject: string
  status: string
  priority: string | null
  created_at: string
  updated_at: string
  assignee_name: string | null
  issue_category: string | null
  symptoms: string | null
  workaround: string | null
  next_steps: string | null
  resolution: string | null
  is_escalated: boolean
  next_follow_up: string | null
  requester_name: string | null
  store: StoreContext | null
  comments: TicketComment[]
}

export interface TicketListItem {
  id: number
  subject: string
  status: string
  priority: string | null
  updated_at: string
  assignee_name: string | null
  issue_category: string | null
  store_id: string | null
  org_name: string | null
}

export interface TicketListResponse {
  tickets: TicketListItem[]
  total: number
  page: number
  per_page: number
  store_id: string | null
  org_name: string | null
}

export interface MyUnsolvedTicketsResponse {
  items: TicketListItem[]
  count: number
  source: 'zendesk_live_cache' | 'bq_snapshot'
  fetched_at: string | null
  expires_at: string | null
  refresh_status: 'fresh' | 'queued' | 'refreshing' | 'failed'
  warning?: string | null
}

export interface KBArticleSource {
  type: 'kb_article'
  title: string
  snippet: string
  url: string
}

export interface TicketSource {
  type: 'ticket'
  id: number
  subject: string
  url: string
}

export type Source = KBArticleSource | TicketSource

export interface BriefSummary {
  overview: string
  current_status: string
  key_actions_taken: string[]
  kb_suggestions: KBArticleSource[]
}

export interface DetailedSummary {
  // Variant B (new format): markdown_text present → render as markdown, ignore legacy fields
  markdown_text?: string | null
  kb_suggestions: KBArticleSource[]
  // Legacy structured fields (backward-compat for old cached data)
  participants?: string[]
  problem_summary?: string
  timeline?: string[]
  root_cause?: string | null
  actions_taken?: string[]
  final_status?: string
  additional_issues?: string[]
  open_questions?: string[]
  next_steps?: string[]
}

export interface SummaryResponse {
  ticket_id: number
  mode: 'brief' | 'detailed'
  summary: BriefSummary | DetailedSummary
  generated_at: string
  from_cache: boolean
}

export interface CustomerRiskMetrics {
  public_customer_comments: number
  public_agent_replies: number
  customer_followups_after_last_agent: number
  last_public_author: 'customer' | 'agent' | 'unknown' | 'none'
  hours_since_last_public_agent_reply: number | null
  hours_since_last_public_customer_comment: number | null
  active_ticket_age_hours: number | null
  is_active: boolean
  is_escalated: boolean
  vendor_dispatched: boolean
  vendor_escalation: boolean
}

export interface CustomerRiskResponse {
  ticket_id: number
  severity: 'green' | 'yellow' | 'orange' | 'red'
  score: number
  label: string
  tone: 'calm' | 'concerned' | 'frustrated' | 'escalated'
  summary: string
  reasons: string[]
  next_action: string
  metrics: CustomerRiskMetrics
  generated_at: string
  from_cache: boolean
  prompt_version: string
  score_version: string
  comment_fingerprint: string
}

export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
}

// ---------------------------------------------------------------------------
// User profile
// ---------------------------------------------------------------------------

export interface TabInfo {
  id: number
  subject: string
  status: string
  priority: string | null
}

export interface SearchHistoryItem {
  query: string
  type: 'ticket' | 'store'
}

export type SidebarSectionKey = 'tools' | 'admin' | 'my_unsolved' | 'open_in_esip'

export type SidebarSections = Partial<Record<SidebarSectionKey, boolean>>

export interface UserProfile {
  uid: string
  email: string
  name: string
  picture: string | null
  theme: 'light' | 'dark'
  search_mode: 'ticket' | 'store'
  open_tabs: TabInfo[]
  search_history: SearchHistoryItem[]
  sidebar_sections?: SidebarSections
}

// ---------------------------------------------------------------------------
// Feedback
// ---------------------------------------------------------------------------

export interface FeedbackEntry {
  id: string
  uid?: string
  text: string
  created_at: string
  updated_at: string
  user_email: string
  user_name: string
  replies: FeedbackReply[]
}

export interface FeedbackReply {
  id: string
  text: string
  author_email: string
  author_name: string
  is_admin_reply: boolean
  created_at: string
}

export interface FeedbackInboxMessage {
  id: string
  type: 'feedback_reply'
  entry_id: string
  reply_id: string
  preview: string
  created_at: string
  read_at: string | null
}

export type Status =
  | 'queued'
  | 'preprocessing'
  | 'running'
  | 'cancelling'
  | 'cancelled'
  | 'completed'
  | 'partial_cancelled'
  | 'partial_failed'
  | 'failed'

export interface PageResponse<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

export interface LanguagePair {
  source_language: string
  source_name: string
  target_language: 'zh'
  target_name: '中文'
  sample_count: number
}

export interface DatasetVersion {
  id: string
  dataset_id: string
  dataset_key: string
  dataset_name: string
  version_label: string
  change_note?: string
  content_sha256: string
  sample_count: number
  source_languages: string[]
  language_pairs: LanguagePair[]
  created_at: string
}

export interface Dataset {
  id: string
  key: string
  name: string
  description: string
  version_count: number
  latest_version: DatasetVersion | null
}

export interface ImportReport {
  id: string
  kind: string
  status: string
  manifest: Record<string, unknown>
  report: {
    valid: boolean
    errors: Array<Record<string, unknown>>
    summary?: Record<string, unknown>
    diff?: Record<string, unknown>
    datasets?: Array<Record<string, unknown>>
    has_manifest?: boolean
    detected_languages?: string[]
    root_predictions?: boolean
    prediction_directories?: string[]
  }
  created_at: string
}

export type ImportOptionCategory = 'model' | 'device' | 'platform' | 'precision' | 'inference_mode'

export interface ImportOption {
  id: string
  category: ImportOptionCategory
  value: string
  label: string
  enabled: boolean
  detects_language: boolean
  platform: string
  sdk: string
  sdk_version: string
  has_results: boolean
}

export interface PromptVersion {
  id: string
  version: number
  version_label: string
  system_template: string
  user_template: string
  published: boolean
  has_results: boolean
  can_delete: boolean
  delete_block_reason: string | null
  created_at: string
}

export interface PromptProfile {
  id: string
  name: string
  description: string
  has_results: boolean
  can_delete: boolean
  delete_block_reason: string | null
  versions: PromptVersion[]
}

export interface EvaluatorRevision {
  id: string
  revision: number
  config: Record<string, unknown>
  default_threshold: number
  created_at: string
}

export interface EvaluatorProfile {
  id: string
  name: string
  evaluator_type: 'openai_compatible_llm' | 'sacrebleu_zh'
  enabled: boolean
  revisions: EvaluatorRevision[]
}

export interface EvaluatorConnection {
  status: 'connected' | 'disconnected'
  detail: string
  latency_ms: number | null
  model_available: boolean | null
}

export interface EvaluatorJob {
  id: string
  name: string
  evaluator_type: string
  revision: number
  prompt_version_id: string | null
  prompt_version_label?: string | null
  status: Status
  total_items: number
  completed_items: number
  cached_items: number
  failed_items: number
  cancelled_items: number
  default_threshold: number
  error: string | null
}

export interface DatasetJob {
  id: string
  dataset_key: string
  version_label: string
  status: Status
  total_items: number
  completed_items: number
  cached_items: number
  failed_items: number
  cancelled_items: number
  cancel_requested: boolean
  evaluator_jobs: EvaluatorJob[]
}

export interface EvaluationTask {
  id: string
  submission_id: string
  run_name: string
  model_family: string
  platform: string
  status: Status
  force_reevaluate: boolean
  total_items: number
  completed_items: number
  cached_items: number
  failed_items: number
  cancelled_items: number
  cancel_requested: boolean
  created_at: string
  started_at: string | null
  finished_at: string | null
  dataset_jobs: DatasetJob[]
}

export interface ThresholdSummary {
  evaluator_job_id: string
  threshold: number
  score_min: number
  score_max: number
  unit: string
  micro_mean: number | null
  macro_mean: number | null
  micro_accuracy: number | null
  macro_accuracy: number | null
  passed: number
  unscored: number
  successful: number
  failed: number
  cancelled: number
  total: number
  coverage: number
  by_language: Array<{
    source_language: string
    mean: number | null
    accuracy: number | null
    passed: number
    unscored: number
    count: number
    total: number
    failed: number
    cancelled: number
    coverage: number
  }>
  aggregates: Array<Record<string, unknown>>
}

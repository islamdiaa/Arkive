// ── API List Response (standardized) ──
export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  limit?: number;
  offset?: number;
  has_more?: boolean;
}

// ── Jobs ──
export interface BackupJob {
  id: string;
  name: string;
  type: 'full' | 'db_dump' | 'flash';
  schedule: string;
  enabled: boolean;
  targets: string[];
  directories: string[];
  include_databases: boolean;
  include_flash: boolean;
  created_at: string;
  updated_at: string;
}

// ── Targets ──
export interface StorageTarget {
  id: string;
  name: string;
  type: 'b2' | 'dropbox' | 'gdrive' | 's3' | 'local' | 'sftp' | 'wasabi';
  enabled: boolean;
  /** Backend writes 'ok' / 'error' after a test; 'unknown' until tested. */
  status: 'ok' | 'healthy' | 'error' | 'unknown' | string;
  last_tested: string | null;
  snapshot_count: number;
  total_size_bytes: number;
}

// ── Job Runs ──
export interface JobRun {
  id: string;
  job_id: string;
  status: 'running' | 'success' | 'partial' | 'failed' | 'cancelled' | 'interrupted' | 'skipped';
  trigger: 'scheduled' | 'manual' | 'cli';
  started_at: string;
  completed_at: string | null;
  duration_seconds: number | null;
  databases_discovered: number;
  databases_dumped: number;
  databases_failed: number;
  total_size_bytes: number;
  /** Human-readable summary for failed and partial runs. */
  error_message?: string | null;
  job_name?: string | null;
  job_type?: 'full' | 'db_dump' | 'flash' | null;
}

// ── Snapshots ──
export interface Snapshot {
  id: string;
  short_id?: string;
  target_id: string;
  full_id?: string;
  time: string;
  hostname: string;
  paths: string[];
  size_bytes: number;
  tags: string[];
}

// ── Discovery ──
export interface DiscoveredContainer {
  name: string;
  image: string;
  status: string;
  databases: DiscoveredDatabase[];
  profile: string | null;
  priority: 'critical' | 'high' | 'medium' | 'low';
}

export interface DiscoveredDatabase {
  container_name: string;
  db_type: 'postgres' | 'sqlite' | 'mysql' | 'mariadb' | 'mongodb' | 'redis';
  db_name: string;
  host_path: string | null;
}

// ── Activity ──
export interface ActivityEntry {
  id: number;
  type: string;
  action: string;
  message: string;
  details: Record<string, unknown>;
  severity: 'info' | 'warning' | 'error' | 'success';
  timestamp: string;
}

// ── Notification Channels ──
export interface NotificationChannel {
  id: string;
  type: string;
  name: string;
  enabled: boolean;
  events: string[];
}

// ── Restore ──
export interface RestorePlan {
  server_name: string;
  generated_at: string;
  targets: StorageTarget[];
  databases: DiscoveredDatabase[];
  flash_available: boolean;
  markdown: string;
}

export interface FileNode {
  name: string;
  path: string;
  type: 'file' | 'dir';
  size?: number;
  children?: FileNode[];
}

// ── Settings ──
export interface ArkiveSettings {
  server_name: string;
  timezone: string;
  retention_days: number;
  keep_daily: number;
  keep_weekly: number;
  keep_monthly: number;
  log_level: 'DEBUG' | 'INFO' | 'WARN' | 'ERROR';
  setup_complete: boolean;
}

// ── Status ──
export interface SystemStatus {
  version: string;
  uptime_seconds: number;
  platform: 'unraid' | 'linux';
  setup_complete: boolean;
  backup_running: boolean;
  last_backup: string | null;
  next_backup: string | null;
  targets_healthy: number;
  targets_total: number;
  databases_discovered: number;
  health: string;
  total_snapshots: number;
  trust_score: number | null;
  last_verified_at: string | null;
  verification_status: VerificationStatus | null;
  coverage: BackupCoverage | null;
  /** Backup freshness watchdog result; a stale job makes status "error". */
  backup_freshness?: BackupFreshness | null;
  /** False until the user confirms they stored a recovery kit off-server. */
  recovery_kit_confirmed?: boolean;
  last_drill?: LastDrill | null;
}

// ── Backup freshness (stale backups) ──
export interface StaleJob {
  job_id: string;
  job_name: string;
  last_good_at: string | null;
  threshold_hours: number | null;
  overdue_hours: number | null;
}

export interface BackupFreshness {
  stale: boolean;
  checked_at: string | null;
  stale_jobs: StaleJob[];
}

// ── Restore drills ──
export type DrillStatus = 'running' | 'passed' | 'failed' | 'skipped';

export interface LastDrill {
  id: string;
  status: DrillStatus | string;
  started_at: string | null;
  completed_at: string | null;
  databases_tested: number;
  databases_passed: number;
  databases_failed: number;
}

export interface RestoreDrill extends LastDrill {
  trigger: string;
  target_id: string | null;
  target_name: string | null;
  snapshot_id: string | null;
  duration_seconds: number | null;
  error_message: string | null;
}

export interface RestoreDrillResult {
  container_name: string;
  db_type: string;
  db_name: string;
  dump_file: string | null;
  dump_size_bytes: number | null;
  status: 'passed' | 'failed' | 'skipped' | string;
  detail: string | null;
  duration_seconds: number | null;
}

export interface RestoreDrillDetail extends RestoreDrill {
  results: RestoreDrillResult[];
}

// ── Recovery kit ──
export interface RecoveryKitTarget {
  id: string;
  name: string;
  type: string;
  repository: string;
  host_path: string | null;
  details: Record<string, unknown>;
}

export interface RecoveryKit {
  generated_at: string;
  server_name: string;
  arkive_version: string;
  encryption_password: string;
  config_keyfile: string | null;
  targets: RecoveryKitTarget[];
  instructions: string[];
  text: string;
}

// ── Monitoring ──
export interface HeartbeatTestResult {
  ok: boolean;
  status_code: number | null;
  error: string | null;
}

// ── Notification event catalog ──
export interface NotificationEventOption {
  value: string;
  label: string;
  description: string;
}

export interface NotificationEventCatalog {
  events: NotificationEventOption[];
  defaults: string[];
}

export interface BackupCoverage {
  readiness: 'migration_ready' | 'partial' | 'minimal';
  migration_ready: boolean;
  appdata_protected: boolean;
  flash_protected: boolean;
  watched_directories: number;
  protected_directories: string[];
  recommended_directories: string[];
  warnings: string[];
}

// ── Storage ──
export interface StorageInfo {
  total_bytes: number;
  used_bytes: number;
  free_bytes: number;
  mount_point: string;
}

// ── Log Entry ──
export interface LogEntry {
  timestamp: string;
  level: string;
  component: string;
  message: string;
}

// ── Verification ──
export interface VerificationRun {
  id: string;
  target_id: string;
  started_at: string;
  completed_at: string | null;
  status: 'running' | 'passed' | 'failed' | 'error';
  files_checked: number;
  files_passed: number;
  databases_checked: number;
  databases_passed: number;
  trust_score: number;
  error_message: string | null;
}

export interface VerificationStatus {
  last_verified_at: string | null;
  trust_score: number | null;
  verification_passing: boolean;
}

// ── SSE Events ──
export type SSEEventType =
  | 'backup:started'
  | 'backup:progress'
  | 'backup:db_complete'
  | 'backup:target_complete'
  | 'backup:completed'
  | 'backup:failed'
  | 'backup:phase'
  | 'backup:cancelled'
  | 'discovery:started'
  | 'discovery:progress'
  | 'discovery:completed'
  | 'restore:started'
  | 'restore:progress'
  | 'restore:completed'
  | 'health:changed'
  | 'verification:started'
  | 'verification:completed'
  | 'verification:failed'
  | 'drill:started'
  | 'drill:completed'
  | 'notification'
  | 'log:entry';

export interface SSEEvent {
  type: SSEEventType;
  data: Record<string, unknown>;
  timestamp: string;
}

-- ============================================================
-- 004_crawl_scope.sql
-- Idempotent – run AFTER 001, 002, 003.
-- Adds a per-source crawl scope: 'domain' (current behaviour, crawl
-- anywhere on the same domain) or 'path' (stay within the seed URL's
-- path prefix, e.g. only /inview/inview-legal/... under a big site).
-- ============================================================

alter table sources
  add column if not exists crawl_scope text not null default 'domain'
    check (crawl_scope in ('domain', 'path'));

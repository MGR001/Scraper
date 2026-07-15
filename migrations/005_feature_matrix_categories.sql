-- ============================================================
-- 005_feature_matrix_categories.sql
-- Idempotent – run AFTER 001-004.
-- Lets a workspace pin a fixed set of Feature Comparison rows (one
-- per line) so they stop reshuffling on every regeneration. Null/empty
-- means fully AI-generated, same as today.
-- ============================================================

alter table workspaces
  add column if not exists feature_matrix_categories text;

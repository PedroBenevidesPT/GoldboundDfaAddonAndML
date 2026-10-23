"""The Postgres schema, as it actually is.

This baseline replaces ``001_initial``, which described four tables that no
longer exist in that shape and used SQLite defaults such as
``datetime('now')``.  It had never been applied -- there was no
``alembic_version`` table -- so nothing depended on it, and keeping it would
have meant a fresh database could not be built from Alembic at all, which is
the one thing a migration tool has to be able to do.

The statements below are the live schema, captured from the running stack with
``pg_dump --schema-only`` after every table the application uses had been
created and the SQLite sources migrated into it.  That makes Alembic the single
authority for the Postgres schema instead of one of several ``executescript``
calls that happened to have run.

Adopting Alembic on a database that already has these objects is a ``stamp``,
not an ``upgrade``: re-running the DDL would fail on the first CREATE TABLE.

    alembic upgrade head     # a database with nothing in it
    alembic stamp head       # a database that already matches this revision
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "002_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: The whole schema: the extension, then tables, then the indexes and
#: constraints that hang off them.  One statement per entry, so a failure
#: names the exact object that broke instead of the whole blob.
STATEMENTS: tuple[str, ...] = (

    "CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public",
    "CREATE TABLE public.aggregate_meta ( key text NOT NULL, value text NOT NULL )",
    "CREATE TABLE public.aggregate_partition ( aggregate text NOT NULL, day text NOT NULL, scope text NOT NULL, source_table text NOT NULL, source_rows bigint NOT NULL, source_checksum text NOT NULL, built_at text NOT NULL )",
    "CREATE TABLE public.ah_auction ( snapshot_id text NOT NULL, auction_key text NOT NULL, auction_id bigint, item_id bigint NOT NULL, item_context bigint, quantity bigint NOT NULL, unit_price_copper bigint, buyout_copper bigint, bid_copper bigint, total_price_copper bigint, unit_price_source text DEFAULT ''::text NOT NULL, time_left_raw text, time_left_band text, is_commodity bigint DEFAULT 0 NOT NULL, pet_species_id bigint, bonus_lists text, modifiers text, price_status text DEFAULT 'missing'::text NOT NULL, price_reason text DEFAULT ''::text NOT NULL, variant_key text )",
    "CREATE TABLE public.ah_realm ( region text NOT NULL, realm_slug text NOT NULL, realm_name text NOT NULL, connected_realm_id integer NOT NULL, refreshed_at text DEFAULT ''::text NOT NULL )",
    "CREATE TABLE public.ah_snapshot ( snapshot_id text NOT NULL, region text NOT NULL, connected_realm_id bigint, realm text NOT NULL, realm_slug text DEFAULT ''::text NOT NULL, captured_at bigint NOT NULL, api_last_modified text DEFAULT ''::text NOT NULL, source text NOT NULL, payload_sha256 text NOT NULL, auction_count bigint DEFAULT 0 NOT NULL, commodity_count bigint DEFAULT 0 NOT NULL, rejected_rows bigint DEFAULT 0 NOT NULL, imported_at text DEFAULT ''::text NOT NULL )",
    "CREATE TABLE public.auction_events ( event_id text NOT NULL, schema_version bigint DEFAULT 1 NOT NULL, event_type text NOT NULL, ts text NOT NULL, timestamp_unix bigint NOT NULL, item_id bigint, item_name text NOT NULL, item_link text, qty bigint DEFAULT 1 NOT NULL, unit_price_copper bigint DEFAULT 0 NOT NULL, total_price_copper bigint DEFAULT 0 NOT NULL, deposit_copper bigint DEFAULT 0 NOT NULL, fee_copper bigint DEFAULT 0 NOT NULL, net_proceeds_copper bigint DEFAULT 0 NOT NULL, realm text NOT NULL, \"character\" text, faction text, counterparty text, source text NOT NULL, provenance text NOT NULL, confidence text NOT NULL, market_ref_copper bigint, raw_json text, imported_at text NOT NULL )",
    "CREATE TABLE public.auction_intel_events ( event_id text NOT NULL, event_type text NOT NULL, ts bigint NOT NULL, item_id bigint, item_name text DEFAULT ''::text NOT NULL, quantity bigint DEFAULT 0 NOT NULL, unit_price_copper bigint DEFAULT 0 NOT NULL, total_price_copper bigint DEFAULT 0 NOT NULL, deposit_copper bigint DEFAULT 0 NOT NULL, consignment_fee_copper bigint DEFAULT 0 NOT NULL, net_proceeds_copper bigint DEFAULT 0 NOT NULL, market_ref_copper bigint DEFAULT 0 NOT NULL, realm text DEFAULT ''::text NOT NULL, \"character\" text DEFAULT ''::text NOT NULL, counterparty text DEFAULT ''::text NOT NULL, source text DEFAULT ''::text NOT NULL, provenance text DEFAULT ''::text NOT NULL, confidence text DEFAULT ''::text NOT NULL, schema_version bigint DEFAULT 0 NOT NULL, captured_at bigint DEFAULT 0 NOT NULL, imported_at text DEFAULT ''::text NOT NULL )",
    "CREATE TABLE public.blizzard_items ( \"itemId\" bigint NOT NULL, name text, quality text, binding text, level bigint, \"sellPrice\" bigint, \"fetchedAt\" text )",
    "CREATE TABLE public.blizzard_missing ( \"itemId\" bigint NOT NULL, \"failedAt\" text )",
    "CREATE TABLE public.daily_ah_auction ( day text NOT NULL, realm text NOT NULL, item_id bigint NOT NULL, n_auctions bigint NOT NULL, n_snapshots bigint NOT NULL, n_commodity bigint NOT NULL, sum_quantity bigint, min_unit_price bigint, max_unit_price bigint, sum_unit_price bigint, sum_total_price bigint )",
    "CREATE TABLE public.daily_event ( day text NOT NULL, source text NOT NULL, event_type text NOT NULL, realm text NOT NULL, n_events bigint NOT NULL, sum_quantity bigint, sum_total bigint, sum_net bigint )",
    "CREATE TABLE public.daily_item_price ( day text NOT NULL, scope text NOT NULL, kind text NOT NULL, item_id bigint NOT NULL, n_observations bigint NOT NULL, n_snapshots bigint NOT NULL, min_market_value bigint, max_market_value bigint, sum_market_value bigint, min_buyout_min bigint, min_buyout_max bigint, sum_min_buyout bigint, sum_avg_sale_price bigint, sum_sale_rate double precision, sum_sold_per_day double precision, last_market_value bigint, last_min_buyout bigint, last_sale_rate double precision, last_sold_per_day double precision, last_snapshot_id bigint )",
    "CREATE TABLE public.db2_item_appearance ( \"itemId\" bigint NOT NULL, \"appearanceId\" bigint NOT NULL )",
    "CREATE TABLE public.db2_items ( \"itemId\" bigint NOT NULL, name text DEFAULT ''::text NOT NULL, binding bigint, quality bigint, expansion bigint, era bigint, \"itemLevel\" bigint, \"requiredLevel\" bigint )",
    "CREATE TABLE public.db2_meta ( key text NOT NULL, value text )",
    "CREATE TABLE public.import_log ( kind text NOT NULL, ref text NOT NULL, imported_at text NOT NULL, detail text DEFAULT ''::text NOT NULL )",
    "CREATE TABLE public.inventory_posted_listing ( snapshot_id text NOT NULL, auction_id bigint DEFAULT 0 NOT NULL, item_id bigint NOT NULL, item_name text DEFAULT ''::text NOT NULL, quantity bigint DEFAULT 1 NOT NULL, unit_price_copper bigint DEFAULT 0 NOT NULL, buyout_copper bigint DEFAULT 0 NOT NULL, time_left bigint DEFAULT 0 NOT NULL )",
    "CREATE TABLE public.inventory_row ( snapshot_id text NOT NULL, location text NOT NULL, item_id bigint NOT NULL, item_name text DEFAULT ''::text NOT NULL, quantity bigint NOT NULL )",
    "CREATE TABLE public.inventory_snapshot ( snapshot_id text NOT NULL, captured_at bigint NOT NULL, \"character\" text DEFAULT ''::text NOT NULL, realm text DEFAULT ''::text NOT NULL, reason text DEFAULT ''::text NOT NULL, source text DEFAULT ''::text NOT NULL, bag_slots bigint DEFAULT 0 NOT NULL, available_json text DEFAULT '[\"bags\",\"bank\",\"posted\"]'::text NOT NULL, imported_at text DEFAULT ''::text NOT NULL )",
    "CREATE TABLE public.item_analytics ( \"snapshotId\" bigint NOT NULL, \"itemId\" bigint NOT NULL, \"farmScore\" double precision NOT NULL, \"goldThroughput\" double precision NOT NULL, \"priceTrend\" double precision NOT NULL, liquidity double precision NOT NULL )",
    "CREATE TABLE public.items ( \"itemId\" bigint NOT NULL, name text NOT NULL, \"createdAt\" text NOT NULL )",
    "CREATE TABLE public.journal ( id bigint NOT NULL, ts text NOT NULL, kind text NOT NULL, \"itemId\" bigint, name text NOT NULL, realm text DEFAULT ''::text NOT NULL, qty bigint DEFAULT 1 NOT NULL, price_copper bigint DEFAULT 0 NOT NULL, note text DEFAULT ''::text NOT NULL )",
    "CREATE TABLE public.journal_stats ( \"itemId\" bigint NOT NULL, name text NOT NULL, realm text NOT NULL, successes bigint DEFAULT 0 NOT NULL, failures bigint DEFAULT 0 NOT NULL, sold_count bigint DEFAULT 0 NOT NULL, sold_copper bigint DEFAULT 0 NOT NULL, bought_count bigint DEFAULT 0 NOT NULL, bought_copper bigint DEFAULT 0 NOT NULL, source text DEFAULT 'journalator'::text NOT NULL, imported_at text NOT NULL )",
    "CREATE TABLE public.mail_invoice ( mail_id text NOT NULL, seen_at bigint NOT NULL, estimated_sale_at bigint DEFAULT 0 NOT NULL, realm text DEFAULT ''::text NOT NULL, \"character\" text DEFAULT ''::text NOT NULL, invoice_type text NOT NULL, item_name text NOT NULL, player_name text NOT NULL, quantity bigint DEFAULT 1 NOT NULL, value_copper bigint DEFAULT 0 NOT NULL, buyout_copper bigint DEFAULT 0 NOT NULL, deposit_copper bigint DEFAULT 0 NOT NULL, consignment_copper bigint DEFAULT 0 NOT NULL, days_left double precision DEFAULT 0 NOT NULL, subject text DEFAULT ''::text NOT NULL, sender text DEFAULT ''::text NOT NULL, item_link text DEFAULT ''::text NOT NULL, source text DEFAULT 'mail_invoice'::text NOT NULL, confidence text DEFAULT 'verified'::text NOT NULL, imported_at text DEFAULT ''::text NOT NULL )",
    "CREATE TABLE public.market_snapshots ( \"snapshotId\" bigint NOT NULL, \"itemId\" bigint NOT NULL, \"marketValue\" double precision NOT NULL, historical double precision NOT NULL, \"avgSalePrice\" double precision NOT NULL, \"saleRate\" double precision NOT NULL, \"soldPerDay\" double precision NOT NULL )",
    "CREATE TABLE public.mob_loot_observation ( observation_id text NOT NULL, item_id bigint NOT NULL, quantity bigint NOT NULL )",
    "CREATE TABLE public.mob_observation ( observation_id text NOT NULL, batch_id text NOT NULL, realm text DEFAULT ''::text NOT NULL, \"character\" text DEFAULT ''::text NOT NULL, npc_id bigint NOT NULL, npc_name text DEFAULT ''::text NOT NULL, kills bigint DEFAULT 0 NOT NULL, loot_events bigint DEFAULT 0 NOT NULL, empty_loot_events bigint DEFAULT 0 NOT NULL, money_copper bigint DEFAULT 0 NOT NULL, first_at bigint DEFAULT 0 NOT NULL, last_at bigint DEFAULT 0 NOT NULL, source text DEFAULT 'game_addon'::text NOT NULL, imported_at text DEFAULT ''::text NOT NULL )",
    "CREATE TABLE public.mob_valuation ( valuation_id text NOT NULL, realm text DEFAULT ''::text NOT NULL, npc_id bigint NOT NULL, npc_name text DEFAULT ''::text NOT NULL, valued_at bigint NOT NULL, price_source text DEFAULT ''::text NOT NULL, kills bigint DEFAULT 0 NOT NULL, money_copper bigint DEFAULT 0 NOT NULL, vendor_copper bigint DEFAULT 0 NOT NULL, market_copper bigint, realized_copper bigint, gold_per_mob_vendor double precision, gold_per_mob_market double precision, gold_per_mob_realized double precision, priced_units bigint DEFAULT 0 NOT NULL, total_units bigint DEFAULT 0 NOT NULL, stale bigint DEFAULT 0 NOT NULL, price_age_h double precision, detail_json text DEFAULT '[]'::text NOT NULL )",
    "CREATE TABLE public.pipeline_lease ( name text NOT NULL, owner text NOT NULL, acquired_at integer NOT NULL, expires_at integer NOT NULL, heartbeat_at integer NOT NULL, runs integer DEFAULT 0 NOT NULL )",
    "CREATE TABLE public.pipeline_run ( run_id text NOT NULL, task text NOT NULL, owner text NOT NULL, started_at bigint NOT NULL, finished_at bigint, ok bigint, detail text DEFAULT ''::text NOT NULL )",
    "CREATE TABLE public.pipeline_stat ( key text NOT NULL, value text NOT NULL, updated_at integer NOT NULL, owner text DEFAULT ''::text NOT NULL )",
    "CREATE TABLE public.realm_model_run ( run_id text NOT NULL, created_at text NOT NULL, region text NOT NULL, realm text NOT NULL, model_kind text NOT NULL, status text NOT NULL, n_rows bigint DEFAULT 0 NOT NULL, metrics_json text DEFAULT '{}'::text NOT NULL, model_path text DEFAULT ''::text NOT NULL, note text DEFAULT ''::text NOT NULL )",
    "CREATE TABLE public.reconciliation ( snapshot_id text NOT NULL, location text NOT NULL, item_id bigint NOT NULL, item_name text DEFAULT ''::text NOT NULL, prev_qty bigint DEFAULT 0 NOT NULL, cur_qty bigint DEFAULT 0 NOT NULL, actual_delta bigint DEFAULT 0 NOT NULL, expected_delta bigint DEFAULT 0 NOT NULL, residual bigint DEFAULT 0 NOT NULL, status text NOT NULL, evidence text DEFAULT ''::text NOT NULL )",
    "CREATE TABLE public.sale_buyer_link ( sale_event_id text NOT NULL, mail_id text NOT NULL, realm text DEFAULT ''::text NOT NULL, buyer text NOT NULL, method text NOT NULL, confidence text NOT NULL, linked_at text NOT NULL, note text DEFAULT ''::text NOT NULL )",
    "CREATE TABLE public.seller_observation ( obs_id text NOT NULL, observed_at bigint NOT NULL, realm text DEFAULT ''::text NOT NULL, seller text NOT NULL, seller_name text NOT NULL, seller_realm text DEFAULT ''::text NOT NULL, item_id bigint, item_name text DEFAULT ''::text NOT NULL, auction_id bigint, unit_price_copper bigint, source text NOT NULL, confidence text NOT NULL, label text DEFAULT ''::text NOT NULL, raw_line text DEFAULT ''::text NOT NULL, \"character\" text DEFAULT ''::text NOT NULL, imported_at text DEFAULT ''::text NOT NULL )",
    "CREATE TABLE public.snapshots ( \"snapshotId\" bigint NOT NULL, \"snapshotTimestamp\" text NOT NULL, \"importFileName\" text, \"rowCount\" bigint NOT NULL, checksum text, \"importedAt\" text NOT NULL )",
    "CREATE TABLE public.tsm_commodity_price ( key_id bigint NOT NULL, name text, market_value bigint, min_buyout bigint, recent bigint, historical bigint, updated_at text, fetched_at text, snapshot_id bigint )",
    "CREATE TABLE public.tsm_market ( \"itemId\" bigint NOT NULL, realm text NOT NULL, min_buyout bigint, market bigint, num_auctions bigint, market_recent bigint, historical bigint )",
    "CREATE TABLE public.tsm_meta ( key text NOT NULL, value text )",
    "CREATE TABLE public.tsm_price_change ( snapshot_id bigint NOT NULL, scope text NOT NULL, kind text NOT NULL, key_id bigint NOT NULL, market_value bigint, min_buyout bigint, recent bigint, historical bigint, avg_sale_price bigint, sale_rate double precision, sold_per_day double precision )",
    "CREATE TABLE public.tsm_realm_group ( realm_slug text NOT NULL, realm_name text, group_id bigint, canonical_slug text NOT NULL, canonical_name text, is_canonical bigint DEFAULT 0 NOT NULL )",
    "CREATE TABLE public.tsm_realm_pet_price ( scope text NOT NULL, pet_species_id bigint NOT NULL, name text, market_value bigint, min_buyout bigint, recent bigint, historical bigint, updated_at text, fetched_at text, snapshot_id bigint )",
    "CREATE TABLE public.tsm_realm_price ( scope text NOT NULL, item_id bigint NOT NULL, name text, market_value bigint, min_buyout bigint, recent bigint, historical bigint, updated_at text, fetched_at text, snapshot_id bigint )",
    "CREATE TABLE public.tsm_region ( \"itemId\" bigint NOT NULL, region_market bigint, region_historical bigint, region_sale_avg bigint, region_sold_per_day double precision, region_sale_rate double precision )",
    "CREATE TABLE public.tsm_region_pet_price ( key_id bigint NOT NULL, name text, market_value bigint, historical bigint, avg_sale_price bigint, sale_rate double precision, sold_per_day double precision, updated_at text, fetched_at text, snapshot_id bigint )",
    "CREATE TABLE public.tsm_region_price ( key_id bigint NOT NULL, name text, market_value bigint, historical bigint, avg_sale_price bigint, sale_rate double precision, sold_per_day double precision, updated_at text, fetched_at text, snapshot_id bigint )",
    "CREATE TABLE public.tsm_snapshot ( snapshot_id bigint NOT NULL, scope text NOT NULL, kind text NOT NULL, updated_at text NOT NULL, fetched_at text NOT NULL, checksum text NOT NULL, rows bigint NOT NULL, changed bigint DEFAULT 0 NOT NULL, path text NOT NULL, etag text DEFAULT ''::text NOT NULL, last_modified text DEFAULT ''::text NOT NULL )",
    "CREATE TABLE public.undercut_observation ( observation_id text NOT NULL, scan_id text DEFAULT ''::text NOT NULL, observed_at bigint NOT NULL, realm text DEFAULT ''::text NOT NULL, \"character\" text DEFAULT ''::text NOT NULL, item_id bigint DEFAULT 0 NOT NULL, item_name text DEFAULT ''::text NOT NULL, item_key text DEFAULT ''::text NOT NULL, item_link text DEFAULT ''::text NOT NULL, own_auction_id bigint DEFAULT 0 NOT NULL, own_quantity bigint DEFAULT 1 NOT NULL, own_buyout_copper bigint DEFAULT 0 NOT NULL, own_unit_price_copper bigint DEFAULT 0 NOT NULL, lowest_unit_price_copper bigint DEFAULT 0 NOT NULL, lowest_seller text DEFAULT ''::text NOT NULL, lowest_seller_realm text DEFAULT ''::text NOT NULL, lowest_auction_id bigint DEFAULT 0 NOT NULL, undercut_copper bigint DEFAULT 0 NOT NULL, undercut_pct double precision DEFAULT 0 NOT NULL, items_ahead bigint DEFAULT 0 NOT NULL, sellers_below bigint DEFAULT 0 NOT NULL, time_left bigint DEFAULT 0 NOT NULL, sellers_json text DEFAULT '[]'::text NOT NULL, source text DEFAULT ''::text NOT NULL, confidence text DEFAULT ''::text NOT NULL, reference_status text DEFAULT ''::text NOT NULL, imported_at text DEFAULT ''::text NOT NULL )",
    "ALTER TABLE ONLY public.aggregate_meta ADD CONSTRAINT aggregate_meta_pkey PRIMARY KEY (key)",
    "ALTER TABLE ONLY public.aggregate_partition ADD CONSTRAINT aggregate_partition_pkey PRIMARY KEY (aggregate, day, scope)",
    "ALTER TABLE ONLY public.ah_auction ADD CONSTRAINT ah_auction_pkey PRIMARY KEY (snapshot_id, auction_key)",
    "ALTER TABLE ONLY public.ah_realm ADD CONSTRAINT ah_realm_pkey PRIMARY KEY (region, realm_slug)",
    "ALTER TABLE ONLY public.ah_snapshot ADD CONSTRAINT ah_snapshot_pkey PRIMARY KEY (snapshot_id)",
    "ALTER TABLE ONLY public.auction_events ADD CONSTRAINT auction_events_pkey PRIMARY KEY (event_id)",
    "ALTER TABLE ONLY public.auction_intel_events ADD CONSTRAINT auction_intel_events_pkey PRIMARY KEY (event_id)",
    "ALTER TABLE ONLY public.blizzard_items ADD CONSTRAINT blizzard_items_pkey PRIMARY KEY (\"itemId\")",
    "ALTER TABLE ONLY public.blizzard_missing ADD CONSTRAINT blizzard_missing_pkey PRIMARY KEY (\"itemId\")",
    "ALTER TABLE ONLY public.daily_ah_auction ADD CONSTRAINT daily_ah_auction_pkey PRIMARY KEY (day, realm, item_id)",
    "ALTER TABLE ONLY public.daily_event ADD CONSTRAINT daily_event_pkey PRIMARY KEY (day, source, event_type, realm)",
    "ALTER TABLE ONLY public.daily_item_price ADD CONSTRAINT daily_item_price_pkey PRIMARY KEY (day, scope, kind, item_id)",
    "ALTER TABLE ONLY public.db2_item_appearance ADD CONSTRAINT db2_item_appearance_pkey PRIMARY KEY (\"itemId\", \"appearanceId\")",
    "ALTER TABLE ONLY public.db2_items ADD CONSTRAINT db2_items_pkey PRIMARY KEY (\"itemId\")",
    "ALTER TABLE ONLY public.db2_meta ADD CONSTRAINT db2_meta_pkey PRIMARY KEY (key)",
    "ALTER TABLE ONLY public.import_log ADD CONSTRAINT import_log_pkey PRIMARY KEY (kind, ref)",
    "ALTER TABLE ONLY public.inventory_posted_listing ADD CONSTRAINT inventory_posted_listing_pkey PRIMARY KEY (snapshot_id, auction_id, item_id)",
    "ALTER TABLE ONLY public.inventory_row ADD CONSTRAINT inventory_row_pkey PRIMARY KEY (snapshot_id, location, item_id)",
    "ALTER TABLE ONLY public.inventory_snapshot ADD CONSTRAINT inventory_snapshot_pkey PRIMARY KEY (snapshot_id)",
    "ALTER TABLE ONLY public.item_analytics ADD CONSTRAINT item_analytics_pkey PRIMARY KEY (\"snapshotId\", \"itemId\")",
    "ALTER TABLE ONLY public.items ADD CONSTRAINT items_pkey PRIMARY KEY (\"itemId\")",
    "ALTER TABLE ONLY public.journal ADD CONSTRAINT journal_pkey PRIMARY KEY (id)",
    "ALTER TABLE ONLY public.journal_stats ADD CONSTRAINT journal_stats_pkey PRIMARY KEY (\"itemId\", realm)",
    "ALTER TABLE ONLY public.mail_invoice ADD CONSTRAINT mail_invoice_pkey PRIMARY KEY (mail_id)",
    "ALTER TABLE ONLY public.market_snapshots ADD CONSTRAINT market_snapshots_pkey PRIMARY KEY (\"snapshotId\", \"itemId\")",
    "ALTER TABLE ONLY public.mob_loot_observation ADD CONSTRAINT mob_loot_observation_pkey PRIMARY KEY (observation_id, item_id)",
    "ALTER TABLE ONLY public.mob_observation ADD CONSTRAINT mob_observation_pkey PRIMARY KEY (observation_id)",
    "ALTER TABLE ONLY public.mob_valuation ADD CONSTRAINT mob_valuation_pkey PRIMARY KEY (valuation_id)",
    "ALTER TABLE ONLY public.pipeline_lease ADD CONSTRAINT pipeline_lease_pkey PRIMARY KEY (name)",
    "ALTER TABLE ONLY public.pipeline_run ADD CONSTRAINT pipeline_run_pkey PRIMARY KEY (run_id)",
    "ALTER TABLE ONLY public.pipeline_stat ADD CONSTRAINT pipeline_stat_pkey PRIMARY KEY (key)",
    "ALTER TABLE ONLY public.realm_model_run ADD CONSTRAINT realm_model_run_pkey PRIMARY KEY (run_id)",
    "ALTER TABLE ONLY public.reconciliation ADD CONSTRAINT reconciliation_pkey PRIMARY KEY (snapshot_id, location, item_id)",
    "ALTER TABLE ONLY public.sale_buyer_link ADD CONSTRAINT sale_buyer_link_pkey PRIMARY KEY (sale_event_id)",
    "ALTER TABLE ONLY public.seller_observation ADD CONSTRAINT seller_observation_pkey PRIMARY KEY (obs_id)",
    "ALTER TABLE ONLY public.snapshots ADD CONSTRAINT snapshots_pkey PRIMARY KEY (\"snapshotId\")",
    "ALTER TABLE ONLY public.tsm_commodity_price ADD CONSTRAINT tsm_commodity_price_pkey PRIMARY KEY (key_id)",
    "ALTER TABLE ONLY public.tsm_market ADD CONSTRAINT tsm_market_pkey PRIMARY KEY (\"itemId\", realm)",
    "ALTER TABLE ONLY public.tsm_meta ADD CONSTRAINT tsm_meta_pkey PRIMARY KEY (key)",
    "ALTER TABLE ONLY public.tsm_price_change ADD CONSTRAINT tsm_price_change_pkey PRIMARY KEY (snapshot_id, key_id)",
    "ALTER TABLE ONLY public.tsm_realm_group ADD CONSTRAINT tsm_realm_group_pkey PRIMARY KEY (realm_slug)",
    "ALTER TABLE ONLY public.tsm_realm_pet_price ADD CONSTRAINT tsm_realm_pet_price_pkey PRIMARY KEY (scope, pet_species_id)",
    "ALTER TABLE ONLY public.tsm_realm_price ADD CONSTRAINT tsm_realm_price_pkey PRIMARY KEY (scope, item_id)",
    "ALTER TABLE ONLY public.tsm_region_pet_price ADD CONSTRAINT tsm_region_pet_price_pkey PRIMARY KEY (key_id)",
    "ALTER TABLE ONLY public.tsm_region ADD CONSTRAINT tsm_region_pkey PRIMARY KEY (\"itemId\")",
    "ALTER TABLE ONLY public.tsm_region_price ADD CONSTRAINT tsm_region_price_pkey PRIMARY KEY (key_id)",
    "ALTER TABLE ONLY public.tsm_snapshot ADD CONSTRAINT tsm_snapshot_pkey PRIMARY KEY (snapshot_id)",
    "ALTER TABLE ONLY public.undercut_observation ADD CONSTRAINT undercut_observation_pkey PRIMARY KEY (observation_id)",
    "CREATE INDEX daily_ah_auction_item ON public.daily_ah_auction USING btree (item_id, day)",
    "CREATE INDEX daily_item_price_item ON public.daily_item_price USING btree (item_id, day)",
    "CREATE INDEX idx_ah_auction_item ON public.ah_auction USING btree (snapshot_id, item_id)",
    "CREATE INDEX idx_ah_auction_price_status ON public.ah_auction USING btree (snapshot_id, price_status)",
    "CREATE INDEX idx_ah_auction_variant ON public.ah_auction USING btree (snapshot_id, variant_key)",
    "CREATE INDEX idx_ah_snapshot_realm ON public.ah_snapshot USING btree (region, realm, captured_at)",
    "CREATE INDEX idx_mail_invoice_facts ON public.mail_invoice USING btree (item_name, quantity, value_copper)",
    "CREATE INDEX idx_mail_invoice_lookup ON public.mail_invoice USING btree (realm, invoice_type, estimated_sale_at)",
    "CREATE INDEX idx_mob_loot_item ON public.mob_loot_observation USING btree (item_id)",
    "CREATE INDEX idx_mob_observation_batch ON public.mob_observation USING btree (batch_id)",
    "CREATE INDEX idx_mob_observation_npc ON public.mob_observation USING btree (realm, npc_id, last_at)",
    "CREATE INDEX idx_mob_valuation_realm ON public.mob_valuation USING btree (realm, npc_id, valued_at)",
    "CREATE INDEX idx_pipeline_run_task ON public.pipeline_run USING btree (task, started_at DESC)",
    "CREATE INDEX idx_seller_item ON public.seller_observation USING btree (item_id, realm, observed_at)",
    "CREATE INDEX idx_seller_name ON public.seller_observation USING btree (seller_name, realm, observed_at)",
    "CREATE INDEX idx_undercut_realm_time ON public.undercut_observation USING btree (realm, observed_at)",
    "CREATE INDEX idx_undercut_seller ON public.undercut_observation USING btree (lowest_seller)",
    "ALTER TABLE ONLY public.ah_auction ADD CONSTRAINT ah_auction_snapshot_id_fkey FOREIGN KEY (snapshot_id) REFERENCES public.ah_snapshot(snapshot_id)",
    "ALTER TABLE ONLY public.inventory_row ADD CONSTRAINT inventory_row_snapshot_id_fkey FOREIGN KEY (snapshot_id) REFERENCES public.inventory_snapshot(snapshot_id)",
    "ALTER TABLE ONLY public.mob_loot_observation ADD CONSTRAINT mob_loot_observation_observation_id_fkey FOREIGN KEY (observation_id) REFERENCES public.mob_observation(observation_id)",
)


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError(
        "this baseline builds a database from nothing; dropping every"
        " table is a deliberate manual act, not a migration")

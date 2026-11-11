-- One-time pre-migration extension for the existing permanent-share table.
-- Run against the reviewed application schema with all share writers stopped.
-- Existing rows receive NULL locators and will be expired by the post-import
-- conversation_share_service_unit_finalize.sql step.
-- DDL implicitly commits and is not idempotent. Back up before execution.

ALTER TABLE swe_chat_shares
    ADD COLUMN publication_id BIGINT UNSIGNED NULL
        COMMENT '不可变分享正文所属publication内部ID',
    ADD COLUMN byte_offset BIGINT UNSIGNED NULL
        COMMENT '分享正文在NAS bundle中的字节偏移',
    ADD COLUMN byte_length BIGINT UNSIGNED NULL
        COMMENT '分享正文字节长度',
    ADD COLUMN service_unit_key VARBINARY(256) NULL
        COMMENT '分享所属NAS服务单元原值；迁移完成后收紧为NOT NULL',
    ADD KEY ix_swe_chat_shares_publication (publication_id),
    ADD KEY ix_swe_chat_shares_service_chat (service_unit_key, chat_id),
    ADD KEY ix_swe_chat_shares_service_creator (service_unit_key, creator_id);

PRAGMA foreign_keys=OFF;
BEGIN TRANSACTION;
CREATE TABLE `message` (
	`id` text PRIMARY KEY,
	`session_id` text NOT NULL,
	`time_created` integer NOT NULL,
	`time_updated` integer NOT NULL,
	`data` text NOT NULL,
	CONSTRAINT `fk_message_session_id_session_id_fk` FOREIGN KEY (`session_id`) REFERENCES `session`(`id`) ON DELETE CASCADE
);
INSERT INTO "message" VALUES('msg_fixture0001','ses_fixture0001',1776224396996,1776224397091,'{"role": "user"}');
INSERT INTO "message" VALUES('msg_fixture0002','ses_fixture0001',1776224397013,1776224678916,'{"role": "assistant", "providerID": "ollama", "modelID": "qwen3.5", "mode": "build", "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}}}');
INSERT INTO "message" VALUES('msg_fixture0003','ses_fixture0001',1776224680919,1776224792102,'{"role": "user"}');
INSERT INTO "message" VALUES('msg_fixture0004','ses_fixture0001',1776224680938,1776224792044,'{"role": "assistant", "providerID": "ollama", "modelID": "gemma4:26b", "finish": "stop", "mode": "build", "tokens": {"total": 38568, "input": 38374, "output": 194, "reasoning": 0, "cache": {"read": 0, "write": 0}}}');
INSERT INTO "message" VALUES('msg_fixture0005','ses_fixture0001',1776224851416,1776225302986,'{"role": "user"}');
INSERT INTO "message" VALUES('msg_fixture0006','ses_fixture0001',1776224851433,1776224989122,'{"role": "assistant", "providerID": "ollama", "modelID": "gemma4:26b", "finish": "tool-calls", "mode": "build", "tokens": {"total": 38834, "input": 38623, "output": 211, "reasoning": 0, "cache": {"read": 0, "write": 0}}}');
INSERT INTO "message" VALUES('msg_fixture0007','ses_fixture0001',1776224989123,1776225147742,'{"role": "assistant", "providerID": "ollama", "modelID": "gemma4:26b", "finish": "tool-calls", "mode": "build", "tokens": {"total": 42117, "input": 41992, "output": 125, "reasoning": 0, "cache": {"read": 0, "write": 0}}}');
INSERT INTO "message" VALUES('msg_fixture0008','ses_fixture0001',1776225147743,1776225302883,'{"role": "assistant", "providerID": "ollama", "modelID": "gemma4:26b", "finish": "stop", "mode": "build", "tokens": {"total": 42483, "input": 42328, "output": 155, "reasoning": 0, "cache": {"read": 0, "write": 0}}}');
INSERT INTO "message" VALUES('msg_fixture0009','ses_fixture0001',1776225221785,1776225660007,'{"role": "user"}');
INSERT INTO "message" VALUES('msg_fixture0010','ses_fixture0001',1776225302884,1776225425502,'{"role": "assistant", "providerID": "ollama", "modelID": "gemma4:26b", "finish": "tool-calls", "mode": "build", "tokens": {"total": 42678, "input": 42648, "output": 30, "reasoning": 0, "cache": {"read": 0, "write": 0}}}');
INSERT INTO "message" VALUES('msg_fixture0011','ses_fixture0001',1776225425503,1776225659900,'{"role": "assistant", "providerID": "ollama", "modelID": "gemma4:26b", "finish": "tool-calls", "mode": "build", "tokens": {"total": 45184, "input": 44815, "output": 369, "reasoning": 0, "cache": {"read": 0, "write": 0}}}');
INSERT INTO "message" VALUES('msg_fixture0012','ses_fixture0001',1776225659901,1776226968170,'{"role": "assistant", "providerID": "ollama", "modelID": "gemma4:26b", "mode": "build", "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}}}');
INSERT INTO "message" VALUES('msg_fixture0013','ses_fixture0001',1776226732673,1776226732677,'{"role": "user"}');
INSERT INTO "message" VALUES('msg_fixture0014','ses_fixture0001',1776232708306,1776232851310,'{"role": "user"}');
INSERT INTO "message" VALUES('msg_fixture0015','ses_fixture0001',1776232708321,1776232851112,'{"role": "assistant", "providerID": "ollama", "modelID": "gemma4:26b", "finish": "stop", "mode": "build", "tokens": {"total": 54542, "input": 54385, "output": 157, "reasoning": 0, "cache": {"read": 0, "write": 0}}}');
CREATE TABLE `session` (
	`id` text PRIMARY KEY,
	`project_id` text NOT NULL,
	`parent_id` text,
	`slug` text NOT NULL,
	`directory` text NOT NULL,
	`title` text NOT NULL,
	`version` text NOT NULL,
	`share_url` text,
	`summary_additions` integer,
	`summary_deletions` integer,
	`summary_files` integer,
	`summary_diffs` text,
	`revert` text,
	`permission` text,
	`time_created` integer NOT NULL,
	`time_updated` integer NOT NULL,
	`time_compacting` integer,
	`time_archived` integer, `workspace_id` text, `path` text, `agent` text, `model` text, `cost` real DEFAULT 0 NOT NULL, `tokens_input` integer DEFAULT 0 NOT NULL, `tokens_output` integer DEFAULT 0 NOT NULL, `tokens_reasoning` integer DEFAULT 0 NOT NULL, `tokens_cache_read` integer DEFAULT 0 NOT NULL, `tokens_cache_write` integer DEFAULT 0 NOT NULL, `metadata` text,
	CONSTRAINT `fk_session_project_id_project_id_fk` FOREIGN KEY (`project_id`) REFERENCES `project`(`id`) ON DELETE CASCADE
);
INSERT INTO "session" VALUES('ses_fixture0001','redacted',NULL,'redacted','/home/user/proj','redacted','redacted',NULL,NULL,NULL,NULL,NULL,NULL,NULL,1776224396943,1776232851247,NULL,NULL,NULL,NULL,NULL,NULL,0.0,0,0,0,0,0,NULL);
CREATE INDEX `session_project_idx` ON `session` (`project_id`);
CREATE INDEX `session_parent_idx` ON `session` (`parent_id`);
CREATE INDEX `session_workspace_idx` ON `session` (`workspace_id`);
CREATE INDEX `message_session_time_created_id_idx` ON `message` (`session_id`,`time_created`,`id`);
COMMIT;

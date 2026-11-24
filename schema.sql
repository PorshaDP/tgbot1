-- Выполните этот скрипт в Supabase:
-- Откройте проект -> SQL Editor -> New query -> Вставьте код -> Run

CREATE TABLE IF NOT EXISTS users (
  telegram_id BIGINT PRIMARY KEY,
  first_name TEXT,
  username TEXT,
  state TEXT DEFAULT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS rooms (
  id BIGSERIAL PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  title TEXT NOT NULL,
  budget TEXT DEFAULT 'Не ограничен',
  admin_id BIGINT NOT NULL,
  status TEXT DEFAULT 'open', -- 'open' | 'drawn'
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS participants (
  id BIGSERIAL PRIMARY KEY,
  room_id BIGINT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
  telegram_id BIGINT NOT NULL,
  display_name TEXT NOT NULL,
  wishlist TEXT DEFAULT 'Пока не указан',
  target_telegram_id BIGINT DEFAULT NULL,
  UNIQUE(room_id, telegram_id)
);

CREATE INDEX IF NOT EXISTS idx_participants_room ON participants(room_id);
CREATE INDEX IF NOT EXISTS idx_participants_user ON participants(telegram_id);

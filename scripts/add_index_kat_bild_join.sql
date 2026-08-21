-- =============================================================
-- Оптимизация: индекс для JOIN-а таблицы data_kat на себя
-- (запрос взрыв-схем на странице Cross OEM / view_parts)
--
-- Проблема:
--   Запрос $_master_diagrams делает self-JOIN data_kat.
--   JOIN условие: (catalog, dir_name, epis_typ, bildtafel2),
--   фильтр результата: bildtafel != ''
--   Без этого индекса MySQL сканирует до 442 строк на ключ
--   JOIN-а чтобы найти нужные строки.
--
-- Решение:
--   Индекс (catalog, dir_name, epis_typ, bildtafel2, bildtafel)
--   — фильтр bildtafel != '' отрабатывает прямо в индексе
--   без чтения строк (Index Condition Pushdown).
--
-- ВАЖНО: основная причина тормозов была в PHP-коде —
--   view_cross_oem_result.php использовал "parts.teilenummer"
--   вместо "parts.teilenummer_suche" в WHERE, вызывая полный
--   скан 18.5M строк (2.2GB). Эта ошибка уже исправлена в PHP.
--   Данный индекс — дополнительная оптимизация JOIN-а.
--
-- Время выполнения: ~15-20 минут (таблица 18.5M строк, 2.2GB).
-- Данные не изменяются, только добавляется индекс.
-- Безопасно выполнять на работающей БД (онлайн DDL InnoDB).
-- =============================================================

-- Проверить — нет ли уже такого индекса
-- SHOW INDEX FROM data_kat WHERE Key_name = 'IDX_kat_bild_join';

ALTER TABLE data_kat
    ADD INDEX IDX_kat_bild_join (catalog, dir_name, epis_typ, bildtafel2, bildtafel);

-- Проверить результат
-- SHOW INDEX FROM data_kat WHERE Key_name = 'IDX_kat_bild_join';

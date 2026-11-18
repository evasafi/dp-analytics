-- Front-page totals: counts and sums over the whole platform, nothing about any one need or person.
CREATE OR REPLACE VIEW public_stats AS
SELECT (SELECT count(*) FROM needs WHERE status = 'published')                         AS open_needs,
       (SELECT count(*) FROM needs WHERE status = 'fulfilled')                         AS fulfilled_needs,
       COALESCE((SELECT sum(amount_try) FROM pledges WHERE status = 'confirmed'), 0)   AS confirmed_try,
       (SELECT count(DISTINCT supporter_id) FROM pledges WHERE status = 'confirmed')   AS donors;
GRANT SELECT ON public_stats TO dp_app;

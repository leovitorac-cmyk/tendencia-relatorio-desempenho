-- Rodar no SQL Editor do Supabase, projeto czqvenpkfsbepggdktfn.
-- Fluxo de justificativa de atraso (1o dia útil do mês) — complementa
-- ucs_gestor/relatorios_recebidos de migration.sql. Uma linha por gestor
-- por ciclo mensal (não por UC — as UCs pendentes já vivem em
-- relatorios_recebidos; o detalhe por UC durante a justificativa é
-- espelhado pro Bubble, não fica no Supabase).

create table if not exists justificativa_ciclos (
  id uuid primary key default gen_random_uuid(),
  mes int not null,
  ano int not null,
  gestor_email text not null,
  gestor_nome text not null,
  token uuid not null default gen_random_uuid(),
  total_itens int not null,
  solicitacao_bubble_id text,
  concluido_em timestamptz,
  analise_enviada boolean not null default false,
  criado_em timestamptz not null default now(),
  unique (mes, ano, gestor_email)
);

create index if not exists justificativa_ciclos_mes_ano_idx
  on justificativa_ciclos (mes, ano, analise_enviada);

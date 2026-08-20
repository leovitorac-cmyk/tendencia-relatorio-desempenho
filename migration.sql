-- Rodar no SQL Editor do Supabase, projeto czqvenpkfsbepggdktfn.
-- Tabelas do módulo relatorio-desempenho — sem relação com as tabelas do motor
-- simple-uno (jobs, fatura_headers, fatura_linhas, tarifas_distribuidora) que já
-- existem nesse mesmo projeto.

create table if not exists ucs_gestor (
  id uuid primary key default gen_random_uuid(),
  uc_codigo text not null unique,
  cliente_nome text,
  gestor_nome text not null,
  gestor_email text not null,
  gestor_bubble_user_id text,
  ativo boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists relatorios_recebidos (
  id uuid primary key default gen_random_uuid(),
  uc_codigo text not null references ucs_gestor(uc_codigo),
  mes int not null,
  ano int not null,
  recebido boolean not null default false,
  data_recebimento timestamptz,
  gmail_message_id text,
  notificado_em timestamptz,
  solicitacao_bubble_id text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (uc_codigo, mes, ano)
);

create index if not exists relatorios_recebidos_mes_ano_recebido_idx
  on relatorios_recebidos (mes, ano, recebido);

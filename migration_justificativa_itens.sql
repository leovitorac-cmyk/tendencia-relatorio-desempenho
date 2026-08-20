-- Rodar no SQL Editor do Supabase, projeto czqvenpkfsbepggdktfn.
-- Substitui o Data Type `JustificativaAtraso` que seria criado no Bubble:
-- 1 linha por UC pendente dentro de um ciclo de justificativa (liga em
-- justificativa_ciclos.token). Ficar no Supabase facilita cruzar direto
-- com ucs_gestor/relatorios_recebidos (mesmo projeto) pra saber quais UCs
-- atrasaram, sem duplicar objeto no Bubble.
--
-- A página pública `justificar-atraso` (sem login, token na URL) lê e
-- escreve nessa tabela DIRETO via REST do Supabase (chave anon, já usada
-- em SUPABASE_PARSE_FATURA_ANON_KEY), sem passar por Edge Function. Quem
-- garante que só o dono do token mexe nos itens dele é a RLS abaixo,
-- lendo o header customizado `x-token` que a página manda em toda
-- chamada — mesmo modelo de "token como senha" do design original em
-- Bubble, só que reforçado no banco (não dá pra ler/escrever item de
-- outro gestor mesmo sabendo a chave anon, porque ela é pública/embutida
-- na página por natureza).

create table if not exists justificativa_itens (
  id uuid primary key default gen_random_uuid(),
  uc_codigo text not null references ucs_gestor(uc_codigo),
  cliente_nome text,
  gestor_nome text not null,
  gestor_email text not null,
  mes int not null,
  ano int not null,
  token uuid not null,
  motivo text check (motivo in (
    'Inscrição estadual baixada',
    'NF com volume divergente da fatura de energia',
    'Volume da fatura divergente da NF',
    'Faturamento a menor da distribuidora',
    'Cliente não recebeu fatura',
    'Distribuidora não emitiu a fatura',
    'Cliente não enviou a fatura/não disponibilizou dados de acesso'
  )),
  respondido boolean not null default false,
  respondido_em timestamptz,
  created_at timestamptz not null default now(),
  unique (uc_codigo, mes, ano),
  check (not respondido or motivo is not null)
);

create index if not exists justificativa_itens_token_respondido_idx
  on justificativa_itens (token, respondido);

-- FK pro ciclo do gestor (garante que todo item pertence a um ciclo real).
do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'justificativa_ciclos_token_key'
  ) then
    alter table justificativa_ciclos add constraint justificativa_ciclos_token_key unique (token);
  end if;
end $$;

do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'justificativa_itens_token_fkey'
  ) then
    alter table justificativa_itens
      add constraint justificativa_itens_token_fkey
      foreign key (token) references justificativa_ciclos(token);
  end if;
end $$;

-- RLS: só os scripts (service key, ignora RLS) escrevem itens novos.
-- A página pública só enxerga/edita os itens cujo token bate com o
-- header x-token que ela mesma manda — nunca a lista inteira da tabela.
alter table justificativa_itens enable row level security;

drop policy if exists justificativa_itens_select_proprio_token on justificativa_itens;
create policy justificativa_itens_select_proprio_token
  on justificativa_itens for select
  to anon
  using (
    token::text = coalesce(current_setting('request.headers', true)::json->>'x-token', '')
  );

drop policy if exists justificativa_itens_update_proprio_token on justificativa_itens;
create policy justificativa_itens_update_proprio_token
  on justificativa_itens for update
  to anon
  using (
    token::text = coalesce(current_setting('request.headers', true)::json->>'x-token', '')
    and respondido = false
  )
  with check (
    token::text = coalesce(current_setting('request.headers', true)::json->>'x-token', '')
    and respondido = true
  );

revoke all on justificativa_itens from anon;
grant usage on schema public to anon;
grant select (id, uc_codigo, cliente_nome, mes, ano, respondido) on justificativa_itens to anon;
grant update (motivo, respondido, respondido_em) on justificativa_itens to anon;

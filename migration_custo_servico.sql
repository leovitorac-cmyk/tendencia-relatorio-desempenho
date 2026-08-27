-- Rodar no SQL Editor do Supabase, projeto czqvenpkfsbepggdktfn.
-- Adiciona coluna com o tipo de cobrança (fixa/variável/híbrida) já
-- presente na planilha original ("solicitação-ucs") mas nunca importada
-- pra ucs_gestor até 2026-08-20. Valores brutos vistos na planilha:
-- "% sobre Economia" (variável), "Valor Fixo" (fixa),
-- "Valor Fixo + % sobre Economia" (híbrida), "Inexistente".

alter table ucs_gestor add column if not exists custo_servico text;

# A troca de Pod que o teste aprovava cedo demais

[English](pod-replacement.en.md)

O [CI 39413620240](https://github.com/tommmdl/chaos-lab/actions/runs/39413620240) conseguiu coletar e atribuir os RST, mas reprovou depois em `Pod replacement completes: drain=True, grace=30, trial=3`. O cluster terminou com duas réplicas prontas. Isso não torna aquela execução aprovada: o relatório original continua com `passed: false`.

## O que encontramos

A espera fazia duas consultas independentes: primeiro conferia dois Pods prontos; depois procurava uma mudança nos UIDs. Entre essas leituras, o Chaos Mesh podia iniciar a remoção e o ReplicaSet criar o substituto. A primeira resposta ainda descrevia o par saudável anterior; a segunda já podia incluir um terceiro Pod, sem prontidão. A expressão resultava em verdadeiro sem ter observado uma substituição completa.

Na tentativa que falhou, o watch registrou a exclusão de `api-5459756fdf-grzwn`, a criação de `api-5459756fdf-cntqs` e o substituto ainda sem prontidão antes do encerramento da coleta. O [resumo desses eventos](../evidence/pod-replacement-historical-analysis.json) aponta para essa saída prematura. As respostas exatas das duas consultas não foram guardadas no teste antigo; não dá para reconstruí-las com certeza depois.

Para conferir o defeito, fizemos uma remoção real com o Chaos Mesh entre as duas leituras da regra antiga. Essa pausa é deliberada: reproduz uma ordem possível sem depender da sorte do agendamento. Não há respostas simuladas do Kubernetes. O teste confirmou que a regra antiga aprovava o estado incompleto e que a nova o rejeitava, aguardando a recuperação real.

## Como ficou a checagem

O módulo compartilhado [pod_replacement.py](../scripts/pod_replacement.py) avalia prontidão e identidade na mesma resposta de listagem. Para a API, o teste só avança quando encontra exatamente dois Pods em execução, todos os containers prontos, nenhum Pod em exclusão e um conjunto de UIDs diferente do inicial. Também cruza o alvo registrado como aplicado com sucesso pelo Chaos Mesh com os Pods iniciais e exige que esse UID tenha desaparecido.

A matriz de resiliência, a investigação TCP e a suíte inicial de experimentos usam o mesmo módulo. A última também confere a substituição do PostgreSQL, esperando um Pod. Depois de remover o recurso do experimento, faz uma nova leitura completa para confirmar o resultado. As suítes de tráfego mantêm a espera de 120 segundos; a suíte inicial mantém a de 100 segundos. Não aumentamos o timeout nem repetimos a suíte até obter uma aprovação.

Cada tentativa guarda o estado inicial, versões dos recursos, observações durante a espera, alvo do experimento e estado após a limpeza. Se houver timeout ou erro, as amostras e os estados já coletados continuam no relatório. A recuperação final também precisa estar saudável para o relatório marcar sucesso.

Isso segue a distinção do Kubernetes entre [identidade, fase, prontidão e encerramento de um Pod](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/): `Running` sozinho não basta para considerar uma réplica recuperada.

## Testes e evidências

O [controle local](../evidence/pod-replacement-local-regression.json) passou nas oito verificações: reproduziu o falso positivo antigo, rejeitou esse estado com a nova regra e confirmou a recuperação. Linhas, timestamps, PVC do PostgreSQL e os recursos dos outros labs continuaram iguais.

A [matriz local de resiliência](../evidence/pod-replacement-local-resilience.json) completou as 16 remoções e passou nas 23 verificações em 270,96 segundos. As observações guardadas confirmam que o UID selecionado desapareceu e havia dois Pods prontos após cada limpeza. Linhas, timestamps e PVC sobreviveram. Os controles reais de HTTP também conferiram o limite de tentativas de GET e o POST já gravado cuja resposta se perdeu.

| Drenagem | Prazo no Chaos | Falhas/requisições diretas | Falhas/requisições GET |
|---|---:|---:|---:|
| não | 0 s | 41/1415 | 3/1882 |
| não | 30 s | 5/2016 | 0/2075 |
| sim | 0 s | 1/1974 | 0/1969 |
| sim | 30 s | 0/1787 | 0/1783 |

A matriz passou nas verificações de recuperação; isso não significa que todas as requisições funcionaram. Três GETs ainda falharam nos cortes abruptos sem drenagem. Suas tentativas e erros continuam nas amostras.

A [suíte TCP local](../evidence/pod-replacement-local-connection-window.json) passou nas 42 verificações ao longo de 12 remoções em 306,57 segundos. Houve 0/5.631 falhas diretas nas remoções graciosas e 2/2.833 nos controles abruptos. Os acessos com conexão de três segundos e 500 ms não tiveram falha final nos cortes abruptos, mas cada um recuperou duas falhas de transporte. As [24 capturas](../evidence/pod-replacement-local-packet-archive.json) foram arquivadas com hashes conferidos: 168.887 quadros, sem drops reportados. Reanalisar os arquivos arquivados produziu exatamente o resultado original: [seis RST atribuídos ao fechamento do listener no Pod da API](../evidence/pod-replacement-local-reset-origins.json), sem observações não resolvidas. São observações desta execução, não uma garantia de disponibilidade.

A [suíte inicial local](../evidence/pod-replacement-local-verification.json) passou depois nas 43 verificações em 202,37 segundos, incluindo a troca da API e do PostgreSQL com o observador compartilhado. O [total local](../evidence/pod-replacement-local-summary.json) ficou em 116 verificações. As três primeiras suítes exercitaram o commit `4bd2eb7`; a suíte inicial exercitou `a4bad03`, que estende a mesma regra às checagens da API e do banco sem alterar os valores padrão da API.

## Primeiro CI depois da correção

O [run 39416708311](https://github.com/tommmdl/chaos-lab/actions/runs/39416708311), commit `4bd2eb7`, passou nas **123 verificações**. O [resumo](../evidence/pod-replacement-ci-39416708311-summary.json) e os relatórios completos guardam as 16 substituições concluídas na matriz, as 12 da suíte TCP e a reprodução do falso positivo antigo. A matriz ainda teve estas falhas HTTP:

| Drenagem | Prazo no Chaos | Falhas/requisições diretas | Falhas/requisições GET |
|---|---:|---:|---:|
| não | 0 s | 5/1222 | 0/1213 |
| não | 30 s | 29/839 | 4/1157 |
| sim | 0 s | 2/1280 | 0/1279 |
| sim | 30 s | 1/1505 | 0/1519 |

Cinco requisições diretas tiveram timeout de cerca de seis segundos, incluindo uma com drenagem e prazo de 30 segundos. Quatro GETs também esgotaram a política de tentativas sem drenagem. Corrigir a checagem de recuperação não explica nem elimina essas falhas. A captura de pacotes pertence à etapa TCP anterior, não a esta matriz; portanto, não permite atribuir a causa desses timeouts posteriores.

A etapa TCP em si não teve falha HTTP final nem RST observado na NodePort. Suas 12 capturas guardaram 164.210 quadros sem drops; os hashes e a reanálise dos arquivos arquivados passaram. Sem reset observado, o resultado de atribuição é `null`, não uma prova de que os resets foram eliminados.

## CI final com todas as checagens corrigidas

O [run 39417441792](https://github.com/tommmdl/chaos-lab/actions/runs/39417441792), commit `a4bad03`, também passou nas **123 verificações**. O [resumo](../evidence/pod-replacement-ci-39417441792-summary.json) guarda as etapas do workflow, o estado final do runner e os resultados dos relatórios que o acompanham. Todas as 16 substituições da matriz e as 12 da suíte TCP foram confirmadas por observações coerentes. A suíte inicial também registrou o desaparecimento dos alvos da API e do PostgreSQL, seguido das réplicas prontas esperadas. As comparações dos dados originais e da identidade do PVC passaram.

A matriz final registrou estas falhas HTTP:

| Drenagem | Prazo no Chaos | Falhas/requisições diretas | Falhas/requisições GET |
|---|---:|---:|---:|
| não | 0 s | 9/1039 | 2/1213 |
| não | 30 s | 19/819 | 4/1185 |
| sim | 0 s | 3/873 | 0/1192 |
| sim | 30 s | 0/1555 | 0/1545 |

As [amostras da matriz](../evidence/pod-replacement-ci-39417441792-resilience.json) incluem **nove timeouts diretos de cerca de seis segundos**, três nos cortes abruptos com drenagem. Seis GETs terminaram em falha sem drenagem. O perfil com drenagem e prazo de 30 segundos não teve falha HTTP final neste run, mas o CI aprovado anterior ainda teve timeout nesse perfil. Os resultados diferentes continuam visíveis; não há uma alegação de conexão sem falhas.

Na [etapa TCP](../evidence/pod-replacement-ci-39417441792-connection-window.json), houve 0/3.235 falhas diretas nas remoções graciosas e 3/1.367 nos controles abruptos. Os acessos de três segundos e 500 ms não tiveram falha final, mas recuperaram três e duas falhas de transporte, respectivamente. As [12 capturas](../evidence/pod-replacement-ci-39417441792-packet-archive.json) guardaram 157.961 quadros sem drops reportados. A [análise](../evidence/pod-replacement-ci-39417441792-reset-origins.json) atribuiu os oito RST observados na NodePort: sete ao fechamento do listener no Pod da API e um a `NO_SOCKET`. Reanalisar os arquivos arquivados reproduziu exatamente o resultado do runner. Essa captura não cobre os timeouts da matriz posterior.

Os dois runs aprovados continuam guardados junto do run anterior que falhou. O workflow não foi repetido sem mudanças para esperar um resultado verde: o segundo run valida a correção adicional nas checagens iniciais da API e do PostgreSQL. Os hashes das capturas foram conferidos, e os relatórios de proveniência guardam os hashes dos JSON originais baixados antes de seus caminhos de captura serem realocados. Os manifestos e estados dos experimentos locais e do CI estão na [pasta de evidências](../evidence/pod-replacement). Cada amostra com erro continua no relatório completo; nenhum erro de transporte ou verificação histórica reprovada foi suprimido.

## Repetir no próprio ambiente

Com o ambiente do lab instalado, execute uma suíte de cada vez:

```bash
python3 tests/pod_replacement.py
python3 tests/resilience.py --rounds 4
python3 tests/connection_window.py --rounds 8 --attribute-resets
```

A captura exige as permissões de tracing e de captura descritas na [investigação dos RST](rst-origin.md). O CI prepara essas dependências num cluster descartável. Os relatórios canônicos são gravados em `evidence`; guarde cada tentativa com outro nome antes de repetir, para não perder o histórico.

Corrigir esta checagem não elimina os erros reais de conexão durante os cortes abruptos. Eles continuam nas amostras HTTP, nas contagens de tentativas e na análise dos RST. O timeout histórico de seis segundos durante remoção graciosa continua sem causa confirmada.

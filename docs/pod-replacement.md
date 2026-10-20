# A troca de Pod que o teste aprovava cedo demais

[English](pod-replacement.en.md)

O [CI 39413620240](https://github.com/tommmdl/chaos-lab/actions/runs/39413620240) conseguiu coletar e atribuir os RST, mas reprovou depois em `Pod replacement completes: drain=True, grace=30, trial=3`. O cluster terminou com duas réplicas prontas. Isso não torna aquela execução aprovada: o relatório original continua com `passed: false`.

## O que encontramos

A espera fazia duas consultas independentes: primeiro conferia dois Pods prontos; depois procurava uma mudança nos UIDs. Entre essas leituras, o Chaos Mesh podia iniciar a remoção e o ReplicaSet criar o substituto. A primeira resposta ainda descrevia o par saudável anterior; a segunda já podia incluir um terceiro Pod, sem prontidão. A expressão resultava em verdadeiro sem ter observado uma substituição completa.

Na tentativa que falhou, o watch registrou a exclusão de `api-5459756fdf-grzwn`, a criação de `api-5459756fdf-cntqs` e o substituto ainda sem prontidão antes do encerramento da coleta. O [resumo desses eventos](../evidence/pod-replacement-historical-analysis.json) aponta para essa saída prematura. As respostas exatas das duas consultas não foram guardadas no teste antigo; não dá para reconstruí-las com certeza depois.

Para conferir o defeito, fizemos uma remoção real com o Chaos Mesh entre as duas leituras da regra antiga. Essa pausa é deliberada: reproduz uma ordem possível sem depender da sorte do agendamento. Não há respostas simuladas do Kubernetes. O teste confirmou que a regra antiga aprovava o estado incompleto e que a nova o rejeitava, aguardando a recuperação real.

## Como ficou a checagem

O módulo compartilhado [pod_replacement.py](../scripts/pod_replacement.py) avalia prontidão e identidade na mesma resposta de listagem. O teste só avança quando encontra exatamente dois Pods em execução, todos os containers prontos, nenhum Pod em exclusão e um conjunto de UIDs diferente do inicial. Também cruza o alvo registrado como aplicado com sucesso pelo Chaos Mesh com os Pods iniciais e exige que esse UID tenha desaparecido.

A matriz de resiliência, a investigação TCP e a suíte inicial de experimentos usam o mesmo módulo. A última também confere a substituição do PostgreSQL, esperando um Pod. Depois de remover o recurso do experimento, faz uma nova leitura completa para confirmar o resultado. O limite continua em 120 segundos. Não aumentamos o timeout nem repetimos a suíte até obter uma aprovação.

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

A matriz passou nas verificações de recuperação; isso não significa que todas as requisições funcionaram. Três GETs ainda falharam nos cortes abruptos sem drenagem. Suas tentativas e erros continuam nas amostras. Os resultados da suíte TCP e do GitHub Actions serão acrescentados após a execução.

## Repetir no próprio ambiente

Com o ambiente do lab instalado, execute uma suíte de cada vez:

```bash
python3 tests/pod_replacement.py
python3 tests/resilience.py --rounds 4
python3 tests/connection_window.py --rounds 8 --attribute-resets
```

A captura exige as permissões de tracing e de captura descritas na [investigação dos RST](rst-origin.md). O CI prepara essas dependências num cluster descartável. Os relatórios canônicos são gravados em `evidence`; guarde cada tentativa com outro nome antes de repetir, para não perder o histórico.

Corrigir esta checagem não elimina os erros reais de conexão durante os cortes abruptos. Eles continuam nas amostras HTTP, nas contagens de tentativas e na análise dos RST. O timeout histórico de seis segundos durante remoção graciosa continua sem causa confirmada.

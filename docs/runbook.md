# Observar, provocar a falha e recuperar

[English](runbook.en.md) · [Início](../README.md)

Comece com `python3 scripts/lab.py status`. Devem existir duas réplicas prontas da API, um PostgreSQL e os quatro componentes de observação. O controlador e o daemon do Chaos Mesh precisam estar saudáveis. Abra o Prometheus e confira `probe_success{job="chaos-http"}`: as duas séries devem valer 1. A caixa de entrada em localhost:8027/api/events mostra os avisos recebidos.

## Aplicar um experimento

Use `python3 scripts/experiment.py apply NOME`, com o nome do arquivo sem `.json`. A primeira experiência pode ser `01-api-pod-kill`. Esse corte mantém prazo zero e ignora o hook de drenagem. Para comparar uma remoção com prazo, use `06-api-graceful-removal`. Guarde os UIDs dos Pods antes e depois. Para rede, use `02-database-delay` ou `03-database-partition`. A indisponibilidade das duas réplicas fica em `04-api-failure`; o reinício do banco, em `05-database-pod-kill`.

O executor verifica o escopo e usa o mesmo lock do teste completo. Cada recurso tem o namespace `chaos-demo` e um seletor com o nome da aplicação e a identificação do lab. `up` recusa reconfigurar o ambiente enquanto houver experimentos registrados. Uma instalação existente de Chaos Mesh que não pertença ao lab também é recusada.

Para acompanhar:

```bash
bash scripts/k.sh get podchaos,networkchaos
bash scripts/k.sh describe networkchaos 03-database-partition
bash scripts/k.sh get pods,pvc
bash scripts/k.sh -n chaos-system logs deployment/chaos-controller-manager --tail=60
bash scripts/k.sh -n chaos-system logs daemonset/chaos-daemon --tail=60
```

`AllInjected` confirma que o controlador conseguiu injetar a falha. Confira também as sondas, o comportamento da aplicação e as mensagens recebidas. `AllRecovered` e sondas saudáveis são parte da verificação de recuperação. O teste de partição aguarda essas condições com o recurso ainda presente e com o mesmo UID, sem apagá-lo para provocar a recuperação.

## Interpretar os sinais

- `ChaosApiSlow`: a sonda de readiness continua funcionando, mas leva mais de 400 ms por pelo menos 9 segundos. O atraso configurado é por pacote; o tempo total da conexão com o banco é maior.
- `ChaosDatabaseUnavailable`: a liveness funciona e a readiness falha. Uma conexão bloqueada pode terminar em timeout da sonda antes de devolver HTTP 503. O experimento conhecido direciona essa falha à conexão com o banco.
- `ChaosApiUnavailable`: as duas sondas falham por pelo menos 9 segundos. Verifique os Pods e o experimento ativo.
- `ChaosProbeMissing`: falta uma das séries de sonda por pelo menos 15 segundos. Confira blackbox e a configuração de coleta. Essa regra não substitui monitoramento externo do próprio Prometheus.

A disponibilidade usa o Service normal da API, que respeita readiness. A observação usa `api-probes`, com `publishNotReadyAddresses=true`, para distinguir processo vivo de dependência indisponível. O Service do PostgreSQL é headless; assim os seletores de Pod dos experimentos alcançam a conexão sem depender da tradução de endereço de um ClusterIP.

O receptor é local e mantém até 500 eventos em SQLite sobre PVC. Ele não envia e-mail nem mensagem para serviços externos. Cada alerta deve ter firing e resolved com o mesmo fingerprint. Se não houver resolved, confira se a falha terminou, se o Prometheus resolveu a regra e se o Alertmanager consegue alcançar o receptor.

## Cancelar e conferir a recuperação

```bash
python3 scripts/experiment.py remove 02-database-delay
python3 scripts/lab.py recover
```

O primeiro comando remove só o experimento escolhido. O segundo remove os PodChaos e NetworkChaos identificados como deste lab. Ambos usam a exclusão normal, aguardando os finalizers. Aguarde o término do teste completo antes de usar outro comando de alteração.

Depois confira os Pods prontos, `probe_success` igual a 1 nos dois endpoints, os alertas resolvidos e os dados. O teste tenta remover as falhas que criou mesmo se uma asserção falhar e registra problemas em `cleanup_errors`. A duração de um experimento depende do controlador; ela não é um temporizador independente dentro do kernel.

Se a limpeza ficar presa, consulte o recurso e os logs. Recupere o controlador e o daemon com a configuração original e refaça a remoção normal. Não remova finalizers à força nem apague CRDs ou o daemon enquanto houver falhas pendentes: isso pode deixar alterações de rede aplicadas. O namespace, os UIDs e os registros dos experimentos ajudam a localizar o alvo correto.

## Encerrar e preservar dados

`recover` mantém workloads e PVCs. Para pausar a aplicação depois de confirmar que não há experimentos, escale a API para zero, espere seus Pods terminarem e só então pare o PostgreSQL. Os componentes de observação também podem ser escalados para zero. Mantenha os PVCs se quiser continuar de onde parou.

A instalação do Helm é `chaos-lab`, no namespace `chaos-system`. Uma desinstalação deve acontecer depois da recuperação de todos os experimentos; ela não remove automaticamente os dados da aplicação. CRDs são recursos do cluster: confira outros usos antes de removê-los. Não há comando local que apague o cluster ou todos os volumes. A limpeza automática do workflow se limita ao cluster descartável criado naquele runner.

Os NodePorts 30434–30437 servem o acesso na rede local do Docker; o processo de navegação publica 8024–8027 apenas em 127.0.0.1. Não há autenticação de produção nesse caminho. Credenciais do banco, kubeconfig, arquivos Helm gerados e referências de dados ficam em `.local`, fora do Git.

## Conferir as conexões durante a remoção

`python3 tests/resilience.py --rounds 4` compara os dois tipos de remoção, com e sem espera, sob tráfego direto e pelo acesso local. O processo de acesso precisa ser reiniciado após atualizar o código. Em localhost:8024, confira `X-Lab-Upstream-Attempts` e `X-Lab-Transport-Failures`; uma leitura recuperada pode esconder uma falha se você olhar apenas o status final. Os relatórios guardam ambos. POST e DELETE nunca são repetidos automaticamente: confira o banco antes de reenviar uma escrita com resultado incerto. [Diagnóstico, números e limites](connections.md).

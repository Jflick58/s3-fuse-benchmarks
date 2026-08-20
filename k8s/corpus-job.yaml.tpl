apiVersion: batch/v1
kind: Job
metadata:
  name: corpus-${RUN_ID}
spec:
  backoffLimit: 0
  template:
    metadata:
      labels: { app: s3fuse-corpus }
    spec:
      restartPolicy: Never
      nodeSelector: { workload: benchmark }
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet
      containers:
        - name: corpus
          image: ${IMAGE}
          args: ["corpus.py", "--bucket", "${BUCKET}", "--region", "${REGION}",
                 "--profile", "${CORPUS_PROFILE}"]
          resources:
            requests: { cpu: "2", memory: "4Gi" }

apiVersion: batch/v1
kind: Job
metadata:
  name: bench-${RUN_ID}
spec:
  backoffLimit: 0
  template:
    metadata:
      labels: { app: s3fuse-bench }
    spec:
      restartPolicy: Never
      nodeSelector: { workload: benchmark }
      # Host networking on purpose. It exposes the real ENA device, which is the
      # only way to read the allowance-exceeded counters that reveal network
      # throttling, and it keeps the CNI datapath out of a measurement that is
      # supposed to be about storage.
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet
      containers:
        - name: bench
          image: ${IMAGE}
          args: ["harness.py", "--bucket", "${BUCKET}", "--region", "${REGION}",
                 "--profile", "${RUN_PROFILE}", "--clients", "${CLIENTS}",
                 "--run-id", "${RUN_ID}", "--out", "/results/results.jsonl"]
          securityContext:
            # Required for FUSE mounts and, critically, for dropping the page
            # cache between repetitions. Without that the benchmark measures RAM.
            privileged: true
          resources:
            requests: { cpu: "2", memory: "8Gi" }
          volumeMounts:
            - { name: fuse,    mountPath: /dev/fuse }
            - { name: nvme,    mountPath: /mnt/nvme,   mountPropagation: HostToContainer }
            - { name: lustre,  mountPath: /mnt/lustre, mountPropagation: HostToContainer }
            - { name: results, mountPath: /results }
            - { name: mounts,  mountPath: /mnt/bench }
      volumes:
        - name: fuse
          hostPath: { path: /dev/fuse, type: CharDevice }
        - name: nvme
          hostPath: { path: /mnt/nvme, type: DirectoryOrCreate }
        # HostToContainer propagation matters here: the node mounts Lustre during
        # bootstrap, and without propagation the pod would keep seeing the empty
        # directory that existed when it started.
        - name: lustre
          hostPath: { path: /mnt/lustre, type: DirectoryOrCreate }
        - name: results
          hostPath: { path: /mnt/nvme/results, type: DirectoryOrCreate }
        - name: mounts
          emptyDir: {}

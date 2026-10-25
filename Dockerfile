FROM ubuntu:22.04

# Avoid prompts during apt installs
ENV DEBIAN_FRONTEND=noninteractive

# Install dependencies including C, C++, Java compilers, Python, Go, Node.js, Git, plus unzip and curl
RUN apt-get update && apt-get install -y \
    build-essential \
    gcc \
    g++ \
    default-jdk \
    python3 \
    python3-pip \
    golang \
    nodejs \
    npm \
    git \
    unzip \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user (matching runAsUser: 1000 in Kubernetes)
RUN useradd -m -u 1000 codemender

# Create workspace and local-work directories and set permissions
RUN mkdir -p /workspace /workspace/local-work && chown -R codemender:codemender /workspace

WORKDIR /opt/codemender

# Copy the codemender zip file (must be in the same directory as the Dockerfile during build)
COPY codemender-cli-v0.1.0-20260515-vMvg-916238397.zip .

# Unzip the client and adjust permissions
RUN unzip codemender-cli-v0.1.0-20260515-vMvg-916238397.zip && \
    rm codemender-cli-v0.1.0-20260515-vMvg-916238397.zip && \
    ln -s /opt/codemender/codemender-cli-v0.1.0-20260515-vMvg-916238397/linux-amd64/cm-linux /usr/local/bin/cm && \
    chown -R codemender:codemender /opt/codemender

# Copy headless scan runner script
COPY --chown=codemender:codemender worker/run_scan.sh /usr/local/bin/run_scan.sh
RUN chmod +x /usr/local/bin/run_scan.sh

# Switch to the non-root user
USER codemender

# Initialize the default codemender workspace
RUN cm init

# Keep warm-pool sandbox alive awaiting orchestrator dispatch or interactive connection
CMD ["sleep", "infinity"]

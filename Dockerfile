# Build the reqired depencies
FROM continuumio/miniconda3 as builder

# Install and package up the conda environment
# Creates a standalone environment in /opt/venv
COPY environment.yml /opt/environment.yml
RUN conda env create -f /opt/environment.yml
RUN conda install -c conda-forge conda-pack poetry=1.8.2
RUN conda-pack -n setup_wrf -o /tmp/env.tar && \
  mkdir /opt/venv && cd /opt/venv && tar xf /tmp/env.tar && \
  rm /tmp/env.tar

# We've put venv in same path it'll be in final image,
# so now fix up paths:
RUN /opt/venv/bin/conda-unpack

# Install the python dependencies using poetry
ENV POETRY_NO_INTERACTION=1 \
    POETRY_HOME='/opt/venv' \
    POETRY_VIRTUALENVS_CREATE=false \
    POETRY_CACHE_DIR=/tmp/poetry_cache

WORKDIR /opt/venv

COPY pyproject.toml poetry.lock ./
RUN touch README.md

# This installs the python dependencies into /opt/venv
RUN --mount=type=cache,target=$POETRY_CACHE_DIR poetry install --no-ansi --no-root

# Container for running the project
# This isn't a hyper optimised container but it's a good starting point
FROM debian:bookworm

MAINTAINER Jared Lewis <jared.lewis@climate-resource.com>

# Configure Python
ENV PYTHONFAULTHANDLER=1 \
  PYTHONUNBUFFERED=1 \
  PYTHONHASHSEED=random

# This is deliberately outside of the work directory
# so that the local directory can be mounted as a volume of testing
ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# Preference the environment libraries over the system libraries
ENV LD_LIBRARY_PATH="/opt/venv/lib:${LD_LIBRARY_PATH}"

WORKDIR /opt/project

# Install additional apt dependencies
RUN apt-get update && \
    apt-get install -y csh bc && \
    rm -rf /var/lib/apt/lists/*

# Copy across the virtual environment
COPY --from=builder /opt/venv /opt/venv

# Copy in WRF and CMAQ binaries
# https://github.com/climate-resource/docker-wrf
# https://github.com/openmethane/docker-cmaq
# TODO: temporarily pinned staging builds until this is verified to work
# Otherwise the CI will be broken for main
COPY --from=ghcr.io/climate-resource/wrf:pr-1 /opt/wrf /opt/wrf
COPY --from=ghcr.io/openmethane/cmaq:pr-2 /opt/cmaq /opt/cmaq

# Copy in the rest of the project
# For testing it might be easier to mount $(PWD):/opt/project so that local changes are reflected in the container
COPY . /opt/project
COPY targets/docker/nccopy_compress_output.sh /opt/project/nccopy_compress_output.sh

# Install the local package in editable mode
#RUN pip install -e .

CMD ["/bin/bash"]
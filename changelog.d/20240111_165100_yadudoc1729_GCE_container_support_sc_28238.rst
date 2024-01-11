New Functionality
^^^^^^^^^^^^^^^^^

- Support for launching workers in containerized environments. Currently
  Docker, Singularity and Apptainer are supported each of which can be used
  by setting the corresponding ``docker_container_uri``, ``singularity_container_uri``,
  or ``apptainer_contianer_uri``. Additional options to be used at container launch
  can be specified with ``container_cmd_options``. Here's a sample config:

.. code-block:: yaml

   display_name: Docker
   engine:
     type: GlobusComputeEngine
     docker_container_uri: funcx/kube-endpoint:main-3.10
     container_cmd_options: -v /tmp:/tmp

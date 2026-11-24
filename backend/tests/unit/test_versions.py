"""Release versions: the backend sends GPU machines the worker image of its own release."""


def test_gpu_machines_run_the_worker_of_the_same_release(monkeypatch):
    import aivo
    from aivo.config import EnvSettings, release_tag

    monkeypatch.setattr(aivo, "__version__", "0.2.57")
    assert release_tag() == "0.2.57"
    assert EnvSettings(_env_file=None).worker_image == "ghcr.io/nltimv/aivo-worker:0.2.57"
    env = EnvSettings(_env_file=None, worker_image_repository="ghcr.io/me/aivo-worker")
    assert env.worker_image == "ghcr.io/me/aivo-worker:0.2.57"
    assert EnvSettings(_env_file=None, worker_image="x/w:1").worker_image == "x/w:1"  # pinned
    monkeypatch.setattr(aivo, "__version__", "dev")  # a source checkout
    assert EnvSettings(_env_file=None).worker_image.endswith(":latest")
